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
  server -> {"type": "typing", "tool"?: str}     (tool: an action is running, e.g. "check_weather")
  server -> {"type": "message", "message": Message}               agent reply with quickReplies
  server -> {"type": "error", "message": "..."}                   recoverable; socket stays open
Close codes: 4401 not authenticated, 4403 no access to this elder.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from app import alerts as alerts_mod  # tests swap its storage
from app import checkin_store, contacts, push, ratelimit
from app.access import load_elder
from app.agent import family, live, messages, translate, voice_push
from app.agent.chips import (
    FALLBACK_FOR_CARETAKER,
    FALLBACK_FOR_ELDER,
    Chip,
    ChipIdea,
    RemindAction,
    caretaker_presets,
    chips_from_ideas,
    elder_presets,
)
from app.agent.family import FamilyMessage
from app.agent.llm import AgentReply, ChatModel, CheckInTurn, Turn, get_chat_model
from app.agent.narrate import narrate, update_card
from app.agent.messages import Message
from app.agent.prompt import build_checkin_prompt, build_system_prompt, clean_quick_replies
from app.auth import AuthedUser, authenticate
from app.checkin import MAX_TURNS, CheckIn, SleepQuality, Mood, assess, checkin_id, merge, next_missing
from app.checkin_store import LogEntry, TodayCheckIn
from app.routers import insights_api
from app.phrases import checkin_opener, checkin_thanks
from app.routers.reminders import all_reminders, save_reminder
from app.routers.settings import load_rules
from app.schedule import Reminder, local_today, reminders_for_day
from app.tools import actions
from app.tools.actions import Action, ActionCard
from app.tools.executor import ToolContext, perform, run_tool, tool_declarations

log = logging.getLogger(__name__)


class Timings:
    """Per-turn latency breakdown, logged as one line (read in Cloud Run logs: 'turn timings')."""

    def __init__(self, what: str, elder_id: str) -> None:
        self.what, self.elder_id, self.t0 = what, elder_id, time.perf_counter()
        self.last, self.parts = self.t0, {}

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.parts[name] = round((now - self.last) * 1000)
        self.last = now

    def log(self, **extra: Any) -> None:
        total = round((time.perf_counter() - self.t0) * 1000)
        log.info("turn timings %s total=%dms %s %s", self.what, total,
                 " ".join(f"{k}={v}ms" for k, v in self.parts.items()), " ".join(f"{k}={v}" for k, v in extra.items() if v))
router = APIRouter()

AUTH_TIMEOUT_S = 10
MODEL_TIMEOUT_S = 45
HISTORY_TURNS = 20
HERE_FRESH_S = 600
HERE_ACCURACY_M = 200
_NON_LATIN = re.compile(r"[\u0900-\u097F\u0E00-\u0E7F\u3040-\u30FF\u4E00-\u9FFF\uAC00-\uD7AF]")  # Devanagari, Thai, CJK, Hangul
HISTORY_ON_CONNECT = 50


def frame_error(kind: str | None, raw: object, exc: ValidationError) -> str:
    """Say what was actually wrong (it used to say '1–1000 characters' for any bad frame), and log it."""
    text = raw.get("text") if isinstance(raw, dict) else None
    log.warning("invalid %s frame from client: %s", kind, [(e["loc"], e["type"]) for e in exc.errors()][:3])
    if kind in ("message", "family_message", "family_reply") and isinstance(text, str):
        if not text.strip():
            return "Please say or type something first."
        if len(text) > 1000:
            return "That message is too long — please keep it under 1000 characters."
    return "Sorry, that didn't go through. Please try again."


def _safe_translate(text: str, target: str, source: str | None, writer: str | None, context: str | None = None) -> str | None:
    try:
        return translate.translate(text, target, source, writer, context)
    except Exception:  # noqa: BLE001 — the original is always shown
        log.warning("family translation failed", exc_info=True)
        return None


def _writer(elder: dict, name: str) -> str:
    """"Priya (daughter)" when the elder's contacts name her with a relationship — for gendered languages."""
    first = name.casefold().split()[:1]
    for c in elder.get("emergencyContacts") or []:
        if first and str(c.get("name", "")).casefold().split()[:1] == first and c.get("relationship"):
            return f"{name} ({c['relationship']})"
    return name


class Here(BaseModel):
    """The phone's location, sent only with ride requests. Kept for this connection, never stored."""
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: float = Field(ge=0, le=1_000_000)  # metres


class TextFrame(BaseModel):
    type: Literal["message"]
    text: str = Field(min_length=1, max_length=1000)
    here: Here | None = None


class StartCheckInFrame(BaseModel):
    type: Literal["start_checkin"]
    mood: Mood | None = None


class CheckInAnswersFrame(BaseModel):
    """The tap check-in: four big buttons instead of a chat. None = skipped."""
    type: Literal["checkin_answers"]
    mood: Mood
    sleepQuality: SleepQuality | None = None
    ate: bool | None = None
    medicationTaken: bool | None = None


class ChipIdeas(AgentReply):
    reply: str = ""
    quickReplies: list[str] = Field(default_factory=list)
    ideas: list[ChipIdea] = Field(default_factory=list)


class FamilyActionFrame(BaseModel):
    type: Literal["family_action"]
    messageId: str
    chip: int


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
        self._here: tuple[Here, float] | None = None  # (location, when it arrived)

    def set_here(self, here: Here | None) -> None:
        if here is not None:
            self._here = (here, time.monotonic())

    def here(self) -> tuple[float, float] | None:
        """A pickup point only if it's from the last 10 minutes and good to ~200 m — else we use home."""
        if self._here is None:
            return None
        h, at = self._here
        return (h.lat, h.lng) if time.monotonic() - at < HERE_FRESH_S and h.accuracy <= HERE_ACCURACY_M else None

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

    async def today_checkin(self, elder: dict[str, Any] | None = None) -> TodayCheckIn | None:
        elder = elder if elder is not None else await self._elder()
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

    async def _reply(self, reply: AgentReply, elder: dict[str, Any], action: Action | None = None) -> None:
        msg = Message(sender="agent", text=reply.reply, quickReplies=reply.quickReplies,
                      action=ActionCard.of(action) if action else None)
        voice_push.speak_reply(self.elder_id, elder, msg.id, msg.text, reply.spoken)  # overlaps the save below
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

    async def chat_turn(self, text: str, elder: dict[str, Any] | None = None) -> None:
        t = Timings("chat_turn", self.elder_id)
        now = dt.datetime.now(dt.UTC)
        elder = elder if elder is not None else await self._elder()
        # Independent reads in parallel (each is a Firestore round-trip): this used to be ~6 in a row.
        rules, people, pending, today, turns = await asyncio.gather(
            asyncio.to_thread(load_rules, self.elder_id, elder),
            asyncio.to_thread(contacts.list_contacts, self.elder_id),
            self._pending_for_elder(),
            self._today_reminders(elder, now),
            self._turns(),
        )
        t.mark("context")
        # Names + relationships only (no phone numbers) so the model can pick who to call or ask.
        elder = {**elder, "_contacts": [f"{p.name} ({p.relationship or p.kind}{', volunteer: ' + ', '.join(p.skills) if p.kind == 'volunteer' else ''})"
                                        for p in people]}
        prompt = build_system_prompt(elder, today, now, pending.summary() if pending else None)
        said_before = [t.text for t in turns if t.role == "elder"][-3:]
        ctx = ToolContext(elder_id=self.elder_id, elder=elder, rules=rules, request_text=text, actor_uid=self.user.uid,
                          people=people, heard=" ".join([*said_before, text]), here=self.here())

        async def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
            if name == "answer_pending_confirmation" and pending is not None:
                return await self.resolve(pending, bool(args.get("approve")), elder)
            # Say what's happening while the tool + the follow-up model call run ("Checking the weather…").
            await self.send({"type": "typing", "tool": name})
            return await run_tool(ctx, name, args)

        reply = await asyncio.wait_for(
            self.model.run(prompt, turns, tool_declarations(pending is not None), execute), MODEL_TIMEOUT_S,
        )
        t.mark("model+tools")
        reply.quickReplies = clean_quick_replies(reply.quickReplies, elder.get("name", ""))
        await self._reply(reply, elder, ctx.touched[-1] if ctx.touched else None)
        t.mark("reply_sent")
        t.log(tool=ctx.touched[-1].tool if ctx.touched else None)

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

    async def checkin_turn(self, c: CheckIn, elder: dict[str, Any] | None = None) -> None:
        t = Timings("checkin_turn", self.elder_id)
        elder, now = elder if elder is not None else await self._elder(), dt.datetime.now(dt.UTC)
        today, turns = await asyncio.gather(self._today_reminders(elder, now), self._turns())
        t.mark("context")
        prompt = build_checkin_prompt(elder, c, today, now)
        turn = await asyncio.wait_for(self.model.complete(prompt, turns, CheckInTurn), MODEL_TIMEOUT_S)
        t.mark("model")
        c = merge(c, turn.extracted).model_copy(update={"turns": c.turns + 1})
        if next_missing(c) is None or c.turns >= MAX_TURNS:
            await self._finish(c, now)
        else:
            await asyncio.to_thread(checkin_store.save_checkin, c)
        await self._reply(turn, elder)  # the elder sees the reply before the status refresh below
        t.mark("reply_sent")
        await self.send_checkin_state()
        t.log()

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
        # Family apps refresh now instead of on their next poll (a worrying answer shouldn't wait 15 s).
        await live.broadcast(self.elder_id, {"type": "checkin_done", "kind": kind, "summary": summary}, "family")
        if kind != "ok":
            elder = await self._elder()
            push.notify_family(elder, f"{'😟' if kind == 'alert' else '👀'} {elder.get('name', '')}'s check-in", summary.removeprefix("Check-in: "))
        # A finished check-in may complete a pattern (e.g. 2nd low-mood day in a row): tell the family once.
        try:
            await asyncio.to_thread(insights_api.new_drift_flags, self.elder_id, await self._elder())
        except Exception:  # noqa: BLE001 — never fail the elder's check-in over an insights hiccup
            log.exception("drift flags failed for elder %s", self.elder_id)

    async def checkin_answers(self, a: CheckInAnswersFrame) -> None:
        """Tap check-in: the elder's answers are the facts — no model needed. Same triage, family log and drift
        flags as the chat check-in (_finish), then a short thank-you in their language."""
        state = await self.today_checkin()
        if state is None:
            await self.send({"type": "error", "message": "Check-ins start after the assessment is done."})
            return
        if state.status == "done":
            await self.send({"type": "error", "message": "You've already checked in today. 💚"})
            return
        elder, now = await self._elder(), dt.datetime.now(dt.UTC)
        c = state.checkIn or CheckIn(id=checkin_id(self.elder_id, state.date), elderId=self.elder_id, date=state.date,
                                     needsMedication=bool(elder.get("medicationSchedule")), startedAt=now)
        meals = None if a.ate is None else ("ate something" if a.ate else "nothing yet")
        c = c.model_copy(update={"mood": a.mood, "sleepQuality": a.sleepQuality, "meals": meals,
                                 "medicationTaken": a.medicationTaken, "turns": c.turns + 1})
        await self._finish(c, now)
        ct_ids = elder.get("caretakerIds") or []
        name = (await asyncio.to_thread(family.caretaker_name, ct_ids[0])).split(" ")[0] if ct_ids else None
        name = None if name == "Family" else name  # caretaker_name's placeholder: say "your family" in their language
        thanks = checkin_thanks(elder.get("language", "en-US"), a.mood, name)
        msg = Message(sender="agent", text=thanks, quickReplies=[])
        voice_push.speak_reply(self.elder_id, elder, msg.id, msg.text)
        await asyncio.to_thread(messages.save, self.elder_id, msg)
        await self.send({"type": "message", "message": msg.model_dump(mode="json")})
        await self.send_checkin_state()

    async def start_checkin(self, mood: Mood | None) -> None:
        state = await self.today_checkin()
        if state is None:
            await self.send({"type": "error", "message": "Check-ins start after the assessment is done."})
            return
        if state.status == "done":
            await self.send({"type": "error", "message": "You've already checked in today. 💚"})
            return
        elder = await self._elder()
        c = state.checkIn
        if c is None:
            c = CheckIn(
                id=checkin_id(self.elder_id, state.date), elderId=self.elder_id, date=state.date,
                needsMedication=bool(elder.get("medicationSchedule")), startedAt=dt.datetime.now(dt.UTC), mood=mood,
            )
            await asyncio.to_thread(checkin_store.save_checkin, c)
        await self.save_elder_message(checkin_opener(elder.get("language", "en-US"), mood))
        await self.send({"type": "typing"})
        await self.checkin_turn(c, elder)

    async def _make_chips(self, elder: dict[str, Any], from_caretaker: bool, sender_name: str, text: str) -> list[Chip]:
        """Chips for whoever receives this family message. Presets first; else the model suggests, code validates."""
        elder_first = (elder.get("name") or "them").split()[0]
        if from_caretaker:
            # Elder can call the sender back only if the family added them to Contacts with a number.
            people = await asyncio.to_thread(contacts.list_contacts, self.elder_id)
            sender_contact = contacts.find_contact(people, sender_name)
            call_name, call_phone = (sender_contact.name, sender_contact.phone) if sender_contact else (None, None)
            preset = elder_presets(text, sender_name, call_phone) if elder.get("language", "en-US") == "en-US" else None
        else:
            call_name, call_phone = elder_first, elder.get("phone") or None
            preset = caretaker_presets(text, elder_first, call_phone)
        if preset:
            return preset

        if from_caretaker:
            ask = (f"{sender_name} (their family) sent them: “{text}”. Suggest 3 replies THEY might tap, in their "
                   "language. Use kind 'remind' when a reminder would genuinely help (e.g. 'Remind me in 30 min'), "
                   f"'call' to phone {sender_name}{'' if call_phone else ' (not available — do not use call)'}, else 'message'.")
        else:
            ask = (f"{elder.get('name', 'The elder')} told their family: “{text}”. Suggest 3 next steps for the family "
                   f"member, in English: 'message' (label is the text sent to {elder_first}), 'remind' (sets a reminder "
                   f"for {elder_first}), or 'call' (phone {elder_first}). Be warm and practical.")
        system = build_system_prompt(elder, [], dt.datetime.now(dt.UTC)) + "\n\n" + ask + (
            " Rules: if the message mentions an appointment, a time or a task, one idea MUST be kind 'remind' with "
            "remindAt (HH:MM, 24h) about an hour before — plus remindDate (YYYY-MM-DD) if it isn't today — or "
            "remindInMinutes for 'later'. Today in their timezone is " + local_today(elder.get("timezone") or "Asia/Kolkata").isoformat() + ". Only the three kinds exist — "
            "never suggest anything else that sounds like an action (no notes, lists, bookings, orders). Labels: max 5 "
            "words, start with an emoji. Put them in `ideas`; reply and quickReplies can be empty.") + (
            "" if from_caretaker else
            " LANGUAGE: these labels are for the family member, who reads English. Write every label in English, "
            "whatever language the rules above say to reply in.")
        try:
            out = await asyncio.wait_for(self.model.complete(system, [Turn("elder", "(suggest)")], ChipIdeas), 15)
            chips = chips_from_ideas(out.ideas, call_name, call_phone)
            if not from_caretaker:  # the family reads English: drop any label the model still wrote in Hindi/Thai
                chips = [c for c in chips if not _NON_LATIN.search(c.label)]
        except Exception:  # noqa: BLE001 — chips are a nicety; never block the message
            log.warning("chip suggestions failed for elder %s", self.elder_id)
            chips = []
        return chips or (FALLBACK_FOR_ELDER if from_caretaker else FALLBACK_FOR_CARETAKER)

    async def on_family(self, text: str) -> None:
        """Caretaker → elder, or elder → caretakers. Delivered to everyone on the family channel."""
        elder = await self._elder()
        # Translate for the receiver (elder's language ↔ English for family) while the chips are being made.
        to_caretaker = self.user.role != "caretaker"
        elder_lang = elder.get("language", "en-US")
        target, source = ("en-US", elder_lang) if to_caretaker else (elder_lang, None)  # family may write any language
        name = await asyncio.to_thread(family.caretaker_name, self.user.uid) if not to_caretaker else ""
        writer = _writer(elder, name) if not to_caretaker else f"{(elder.get('name') or 'the elder').split()[0]} (the older family member)"
        # What this answers — the other side's last message — so a short reply isn't read as a question.
        other = "caretaker" if to_caretaker else "elder"
        last = next((m for m in reversed(await asyncio.to_thread(family.recent, self.elder_id, 6)) if m.sender == other), None)
        tr = asyncio.create_task(asyncio.to_thread(_safe_translate, text, target, source, writer, last.text if last else None))
        if self.user.role == "caretaker":
            msg = FamilyMessage(sender="caretaker", senderUid=self.user.uid, senderName=name, text=text,
                                chips=await self._make_chips(elder, True, name, text))
        else:
            msg = FamilyMessage(sender="elder", senderUid=self.user.uid, senderName=elder.get("name", ""), text=text,
                                chips=await self._make_chips(elder, False, elder.get("name", ""), text))
        translated = await tr
        if translated:
            msg.translated, msg.translatedTo = translated, target
        await asyncio.to_thread(family.save, self.elder_id, msg)
        await live.broadcast(self.elder_id, {"type": "family_message", "message": msg.model_dump(mode="json")}, "family")
        if self.user.role == "elder":
            push.notify_family(elder, f"💬 {elder.get('name', '')}", text, "/caretaker/chat")

    async def on_family_action(self, message_id: str, chip_index: int) -> None:
        """Carry out an action chip. Checked: right message, right recipient, not used, a known action."""
        msg = await asyncio.to_thread(family.get, self.elder_id, message_id)
        recipient = {"caretaker": "elder", "elder": "caretaker"}.get(msg.sender) if msg else None
        if msg is None or recipient != self.user.role or not 0 <= chip_index < len(msg.chips):
            await self.send({"type": "error", "message": "That option isn't available."})
            return
        chip = msg.chips[chip_index]
        if chip_index in msg.usedChips:
            await self.send({"type": "error", "message": "That's already been done."})
            return
        if not isinstance(chip.action, RemindAction):
            # Messages are sent as normal text; calls are a tap-to-call link on the device.
            await self.send({"type": "error", "message": "Nothing to run for that option."})
            return
        # Claim first, act second: a double tap (or two caretakers at once) can't set two reminders.
        if not await asyncio.to_thread(family.mark_used, self.elder_id, message_id, chip_index):
            await self.send({"type": "error", "message": "That's already been done."})
            return
        elder = await self._elder()
        try:
            day, time_hhmm = chip.action.when(elder.get("timezone") or "Asia/Kolkata", dt.datetime.now(dt.UTC))
        except ValueError:
            await self.send({"type": "error", "message": "That time has already passed."})
            return
        await asyncio.to_thread(save_reminder, self.elder_id, chip.action.title, time_hhmm, day)
        who = "you" if self.user.role == "elder" else (await asyncio.to_thread(family.caretaker_name, self.user.uid))
        when_label = time_hhmm if day == local_today(elder.get("timezone") or "Asia/Kolkata") else f"{time_hhmm} tomorrow"
        note = FamilyMessage(sender="system", senderUid=self.user.uid, senderName="Elderly",
                             text=f"⏰ Reminder set for {when_label} — {chip.action.title} (by {who})")
        await asyncio.to_thread(family.save, self.elder_id, note)
        updated = msg.model_copy(update={"usedChips": sorted({*msg.usedChips, chip_index})})
        await live.broadcast(self.elder_id, {"type": "family_update", "message": updated.model_dump(mode="json")}, "family")
        await live.broadcast(self.elder_id, {"type": "family_message", "message": note.model_dump(mode="json")}, "family")

    async def on_text(self, text: str) -> None:
        # Save the elder's message and load what we need at the same time; "typing" goes out as soon as it's saved.
        elder = await self._elder()
        _, state = await asyncio.gather(self.save_elder_message(text), self.today_checkin(elder))
        await self.send({"type": "typing"})
        if state is not None and state.checkIn is not None and state.checkIn.status == "in_progress":
            await self.checkin_turn(state.checkIn, elder)
        else:
            await self.chat_turn(text, elder)


async def _authenticate(ws: WebSocket, elder_id: str) -> AuthedUser | None:
    try:
        first: Any = await asyncio.wait_for(ws.receive_json(), AUTH_TIMEOUT_S)
    except WebSocketDisconnect:
        # Client left before signing in (React dev double-mount, tab backgrounded, network switch). Normal.
        return None
    except (TimeoutError, ValueError):
        await ws.close(code=4401, reason="Not authenticated")
        return None
    try:
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

    try:
        if is_elder:
            history = await asyncio.to_thread(messages.recent, elder_id, HISTORY_ON_CONNECT)
            await s.send({"type": "history", "messages": [m.model_dump(mode="json") for m in history]})
        fam = await asyncio.to_thread(family.recent, elder_id, HISTORY_ON_CONNECT)
        await s.send({"type": "family_history", "messages": [m.model_dump(mode="json") for m in fam]})
        if is_elder:
            await s.send_checkin_state()

        while True:
            raw = await ws.receive_json()
            kind = raw.get("type") if isinstance(raw, dict) else None
            if not ratelimit.chat_frames.allow(user.uid):
                await s.send({"type": "error", "message": "You're sending messages very quickly — please wait a moment."})
                continue
            try:
                if kind == "family_action":
                    frame_a = FamilyActionFrame.model_validate(raw)
                    await s.on_family_action(frame_a.messageId, frame_a.chip)
                elif kind in ("family_message", "family_reply"):
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
                    s.set_here(frame.here)
                    await s.on_text(frame.text.strip())
                elif kind == "start_checkin":
                    await s.start_checkin(StartCheckInFrame.model_validate(raw).mood)
                elif kind == "checkin_answers":
                    await s.checkin_answers(CheckInAnswersFrame.model_validate(raw))
                elif kind == "confirm":
                    frame_c = ConfirmFrame.model_validate(raw)
                    await s.on_confirm(frame_c.actionId, frame_c.approve)
            except ValidationError as exc:
                await s.send({"type": "error", "message": frame_error(kind, raw, exc)})
            except WebSocketDisconnect:
                raise
            except Exception:
                log.exception("chat turn failed for elder %s", elder_id)
                await s.send({"type": "error", "message": "Sorry, I couldn't answer just now. Please try again."})
    except WebSocketDisconnect:
        return
    finally:
        live.unregister(elder_id, s.send)
