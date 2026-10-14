"""Elder <-> agent chat over WebSocket, including the guided daily check-in.

Protocol (JSON frames):
  client -> {"type": "auth", "token": "<Firebase ID token>"}      must be first, within 10s
  Elders get the agent thread + family thread. Caretakers get the family thread ONLY
  (the agent conversation is private unless full_access, and that's read via Firestore rules).
  server -> {"type": "history", "messages": [Message, ...]}         elders only
  server -> {"type": "family_history", "messages": [FamilyMessage, ...]}
  client -> {"type": "family_message", "text": "..."}             caretaker → elder
  client -> {"type": "family_reply", "text": "..."}               elder → caretakers
  server -> {"type": "family_message", "message": FamilyMessage}
  server -> {"type": "checkin", "checkin": TodayCheckIn | null}   on connect and after each check-in turn
  client -> {"type": "message", "text": "..."}
  client -> {"type": "start_checkin", "mood": "good" | null}
  client -> {"type": "confirm", "actionId": "...", "approve": true}   Yes/No on an action card
  server -> {"type": "action_update", "messageId": "...", "action": ActionCard}
  server -> {"type": "message", "message": Message}               saved elder message (echo)
  server -> {"type": "typing"}
  server -> {"type": "message", "message": Message}               agent reply with quickReplies
  server -> {"type": "error", "message": "..."}                   recoverable; socket stays open
Close codes: 4401 not authenticated, 4403 no access to this elder.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from app import checkin_store
from app.access import load_elder
from app.agent import family, live, messages
from app.agent.family import FALLBACK_REPLIES, PRESET_REPLIES, FamilyMessage
from app.agent.llm import AgentReply, ChatModel, CheckInTurn, Turn, get_chat_model
from app.agent.narrate import narrate, update_card
from app.agent.messages import Message
from app.agent.prompt import build_checkin_prompt, build_system_prompt
from app.auth import AuthedUser, authenticate
from app.checkin import MAX_TURNS, MOOD_LABEL, CheckIn, Mood, assess, checkin_id, merge, next_missing
from app.checkin_store import LogEntry, TodayCheckIn
from app.routers.reminders import all_reminders
from app.routers.settings import load_rules
from app.schedule import Reminder, local_today, reminders_for_day
from app.tools import actions
from app.tools.actions import Action, ActionCard
from app.tools.executor import ToolContext, perform, run_tool, tool_declarations

log = logging.getLogger(__name__)
router = APIRouter()

AUTH_TIMEOUT_S = 10
MODEL_TIMEOUT_S = 45
HISTORY_TURNS = 20
HISTORY_ON_CONNECT = 50


class TextFrame(BaseModel):
    type: Literal["message"]
    text: str = Field(min_length=1, max_length=1000)


class StartCheckInFrame(BaseModel):
    type: Literal["start_checkin"]
    mood: Mood | None = None


class FamilyFrame(BaseModel):
    type: Literal["family_message", "family_reply"]
    text: str = Field(min_length=1, max_length=1000)


class ConfirmFrame(BaseModel):
    type: Literal["confirm"]
    actionId: str
    approve: bool


class Session:
    """One connected client talking for one elder."""

    def __init__(self, ws: WebSocket, user: AuthedUser, elder_id: str, model: ChatModel) -> None:
        self.ws, self.user, self.elder_id, self.model = ws, user, elder_id, model

    async def send(self, frame: dict[str, Any]) -> None:
        await self.ws.send_json(frame)

    async def _elder(self) -> dict[str, Any]:
        return await asyncio.to_thread(load_elder, self.user, self.elder_id)  # fresh each turn

    async def _today_reminders(self, elder: dict[str, Any], now: dt.datetime) -> list[Reminder]:
        tz = elder.get("timezone")
        if tz is None:
            return []
        everything = await asyncio.to_thread(all_reminders, self.elder_id)
        return reminders_for_day(everything, local_today(tz, now))

    async def today_checkin(self) -> TodayCheckIn | None:
        elder = await self._elder()
        return await asyncio.to_thread(checkin_store.today_checkin, self.elder_id, elder, dt.datetime.now(dt.UTC))

    async def send_checkin_state(self) -> None:
        state = await self.today_checkin()
        await self.send({"type": "checkin", "checkin": state.model_dump(mode="json") if state else None})

    async def _turns(self) -> list[Turn]:
        history = await asyncio.to_thread(messages.recent, self.elder_id, HISTORY_TURNS)
        turns = [Turn(role=m.sender, text=m.text) for m in history]
        while turns and turns[0].role == "agent":  # Gemini expects a user turn first
            turns.pop(0)
        return turns

    async def save_elder_message(self, text: str) -> None:
        msg = Message(sender="elder", text=text, actedBy=self.user.uid if self.user.role == "caretaker" else None)
        await asyncio.to_thread(messages.save, self.elder_id, msg)
        await self.send({"type": "message", "message": msg.model_dump(mode="json")})

    async def _reply(self, reply: AgentReply, action: Action | None = None) -> None:
        msg = Message(sender="agent", text=reply.reply, quickReplies=reply.quickReplies,
                      action=ActionCard.of(action) if action else None)
        await asyncio.to_thread(messages.save, self.elder_id, msg)
        if action is not None:
            action.messageId = msg.id
            await asyncio.to_thread(actions.save, action)
        await self.send({"type": "message", "message": msg.model_dump(mode="json")})

    async def _pending_for_elder(self) -> Action | None:
        waiting = await asyncio.to_thread(actions.with_status, self.elder_id, "awaiting_elder")
        return waiting[-1] if waiting else None

    async def resolve(self, a: Action, approve: bool, elder: dict[str, Any]) -> dict[str, Any]:
        """Elder said Yes/No to an action (tap or voice). Deterministic — no model involved in the decision."""
        fresh = await asyncio.to_thread(actions.get, self.elder_id, a.id)
        if fresh is None or fresh.status != "awaiting_elder":
            return {"status": "already_decided"}
        fresh.decidedBy = self.user.uid
        if approve:
            result = await perform(fresh, elder)
        else:
            fresh.status = "cancelled"
            await asyncio.to_thread(actions.save, fresh)
            result = {"status": "cancelled", "say": "Okay, I won't do that."}
        await update_card(fresh)
        return result

    async def chat_turn(self, text: str) -> None:
        elder, now = await self._elder(), dt.datetime.now(dt.UTC)
        rules = await asyncio.to_thread(load_rules, self.elder_id, elder)
        pending = await self._pending_for_elder()
        prompt = build_system_prompt(elder, await self._today_reminders(elder, now), now, pending.summary() if pending else None)
        ctx = ToolContext(elder_id=self.elder_id, elder=elder, rules=rules, request_text=text, actor_uid=self.user.uid)

        async def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
            if name == "answer_pending_confirmation" and pending is not None:
                return await self.resolve(pending, bool(args.get("approve")), elder)
            return await run_tool(ctx, name, args)

        reply = await asyncio.wait_for(
            self.model.run(prompt, await self._turns(), tool_declarations(pending is not None), execute), MODEL_TIMEOUT_S,
        )
        await self._reply(reply, ctx.touched[-1] if ctx.touched else None)

    async def on_confirm(self, action_id: str, approve: bool) -> None:
        a = await asyncio.to_thread(actions.get, self.elder_id, action_id)
        if a is None:
            await self.send({"type": "error", "message": "That request has expired."})
            return
        elder = await self._elder()
        await self.send({"type": "typing"})
        result = await self.resolve(a, approve, elder)
        if result["status"] == "already_decided":
            await self.send({"type": "error", "message": "That's already been decided."})
            return
        await narrate(self.model, elder, self.elder_id, f"{a.summary()} → {result}", str(result.get("say", "Okay.")))

    async def checkin_turn(self, c: CheckIn) -> None:
        elder, now = await self._elder(), dt.datetime.now(dt.UTC)
        prompt = build_checkin_prompt(elder, c, await self._today_reminders(elder, now), now)
        turn = await asyncio.wait_for(self.model.complete(prompt, await self._turns(), CheckInTurn), MODEL_TIMEOUT_S)
        c = merge(c, turn.extracted).model_copy(update={"turns": c.turns + 1})
        if next_missing(c) is None or c.turns >= MAX_TURNS:
            await self._finish(c, now)
        else:
            await asyncio.to_thread(checkin_store.save_checkin, c)
        await self._reply(turn)
        await self.send_checkin_state()

    async def _finish(self, c: CheckIn, now: dt.datetime) -> None:
        kind, escalate, summary = assess(c)
        c = c.model_copy(update={"status": "done", "completedAt": now, "escalateFlag": escalate})
        await asyncio.to_thread(checkin_store.save_checkin, c)
        history = await asyncio.to_thread(messages.recent, self.elder_id, HISTORY_ON_CONNECT)
        transcript = "\n".join(
            f"{'Elder' if m.sender == 'elder' else 'Elderly'}: {m.text}" for m in history if m.createdAt >= c.startedAt
        )
        await asyncio.to_thread(checkin_store.save_transcript, c, transcript)
        await asyncio.to_thread(
            checkin_store.add_log, self.elder_id, LogEntry(message=summary, kind=kind, checkInId=c.id)
        )

    async def start_checkin(self, mood: Mood | None) -> None:
        state = await self.today_checkin()
        if state is None:
            await self.send({"type": "error", "message": "Check-ins start after the assessment is done."})
            return
        if state.status == "done":
            await self.send({"type": "error", "message": "You've already checked in today. 💚"})
            return
        c = state.checkIn
        if c is None:
            elder = await self._elder()
            c = CheckIn(
                id=checkin_id(self.elder_id, state.date), elderId=self.elder_id, date=state.date,
                needsMedication=bool(elder.get("medicationSchedule")), startedAt=dt.datetime.now(dt.UTC), mood=mood,
            )
            await asyncio.to_thread(checkin_store.save_checkin, c)
        await self.save_elder_message(f"I'm feeling {MOOD_LABEL[mood]}" if mood else "Let's do my check-in")
        await self.send({"type": "typing"})
        await self.checkin_turn(c)

    async def _suggest_replies(self, elder: dict[str, Any], sender_name: str, text: str) -> list[str]:
        preset = PRESET_REPLIES.get(text.strip().lower())
        if preset and elder.get("language", "en-US") == "en-US":
            return preset
        system = build_system_prompt(elder, [], dt.datetime.now(dt.UTC)) + (
            f"\n\n{sender_name} (their family) just sent them: “{text}”. Suggest 3 short replies THEY might tap "
            "to answer {sender_name}, in their language, max 5 words, starting with a fitting emoji. "
            "Put the suggestions in quickReplies; reply can be empty."
        )
        try:
            out = await asyncio.wait_for(
                self.model.complete(system, [Turn("elder", "(suggest my replies)")], AgentReply), 15)
            return out.quickReplies or FALLBACK_REPLIES
        except Exception:  # noqa: BLE001 — suggestions are a nicety; never block the message
            log.warning("reply suggestions failed for elder %s", self.elder_id)
            return FALLBACK_REPLIES

    async def on_family(self, text: str) -> None:
        """Caretaker → elder, or elder → caretakers. Delivered to everyone on the family channel."""
        if self.user.role == "caretaker":
            elder = await self._elder()
            name = await asyncio.to_thread(family.caretaker_name, self.user.uid)
            msg = FamilyMessage(sender="caretaker", senderUid=self.user.uid, senderName=name, text=text,
                                quickReplies=await self._suggest_replies(elder, name, text))
        else:
            elder = await self._elder()
            msg = FamilyMessage(sender="elder", senderUid=self.user.uid, senderName=elder.get("name", ""), text=text)
        await asyncio.to_thread(family.save, self.elder_id, msg)
        await live.broadcast(self.elder_id, {"type": "family_message", "message": msg.model_dump(mode="json")}, "family")

    async def on_text(self, text: str) -> None:
        await self.save_elder_message(text)
        await self.send({"type": "typing"})
        state = await self.today_checkin()
        if state is not None and state.checkIn is not None and state.checkIn.status == "in_progress":
            await self.checkin_turn(state.checkIn)
        else:
            await self.chat_turn(text)


async def _authenticate(ws: WebSocket, elder_id: str) -> AuthedUser | None:
    try:
        first: Any = await asyncio.wait_for(ws.receive_json(), AUTH_TIMEOUT_S)
        if not isinstance(first, dict) or first.get("type") != "auth" or not isinstance(first.get("token"), str):
            raise HTTPException(401)
        user = await asyncio.to_thread(authenticate, first["token"])
    except (TimeoutError, ValueError, HTTPException):
        await ws.close(code=4401, reason="Not authenticated")
        return None
    try:
        await asyncio.to_thread(load_elder, user, elder_id)
    except HTTPException:
        await ws.close(code=4403, reason="No access")
        return None
    return user


@router.websocket("/ws/{elder_id}")
async def chat(ws: WebSocket, elder_id: str, model: Annotated[ChatModel, Depends(get_chat_model)]) -> None:
    await ws.accept()
    user = await _authenticate(ws, elder_id)
    if user is None:
        return
    s = Session(ws, user, elder_id, model)
    is_elder = user.role == "elder"
    live.register(elder_id, s.send, frozenset({"agent", "family"}) if is_elder else frozenset({"family"}))

    if is_elder:
        history = await asyncio.to_thread(messages.recent, elder_id, HISTORY_ON_CONNECT)
        await s.send({"type": "history", "messages": [m.model_dump(mode="json") for m in history]})
    fam = await asyncio.to_thread(family.recent, elder_id, HISTORY_ON_CONNECT)
    await s.send({"type": "family_history", "messages": [m.model_dump(mode="json") for m in fam]})
    if is_elder:
        await s.send_checkin_state()

    try:
        while True:
            raw = await ws.receive_json()
            kind = raw.get("type") if isinstance(raw, dict) else None
            try:
                if kind in ("family_message", "family_reply"):
                    frame_f = FamilyFrame.model_validate(raw)
                    if (frame_f.type == "family_message") != (user.role == "caretaker"):
                        await s.send({"type": "error", "message": "Wrong message type for your role"})
                        continue
                    await s.on_family(frame_f.text.strip())
                elif not is_elder:
                    # Caretakers only use the family channel (Act-as-Elder would be a separate, explicit mode).
                    await s.send({"type": "error", "message": "Caretakers can only send family messages here"})
                elif kind == "message":
                    frame = TextFrame.model_validate(raw)
                    await s.on_text(frame.text.strip())
                elif kind == "start_checkin":
                    await s.start_checkin(StartCheckInFrame.model_validate(raw).mood)
                elif kind == "confirm":
                    frame_c = ConfirmFrame.model_validate(raw)
                    await s.on_confirm(frame_c.actionId, frame_c.approve)
            except ValidationError:
                await s.send({"type": "error", "message": "Message must be 1–1000 characters"})
            except WebSocketDisconnect:
                raise
            except Exception:
                log.exception("chat turn failed for elder %s", elder_id)
                await s.send({"type": "error", "message": "Sorry, I couldn't answer just now. Please try again."})
    except WebSocketDisconnect:
        return
    finally:
        live.unregister(elder_id, s.send)
