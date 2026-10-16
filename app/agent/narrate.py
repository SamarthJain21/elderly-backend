"""Turn an event (order placed, family approved…) into a short agent message in the elder's language."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

from app.agent import live, messages, voice_push
from app.agent.llm import AgentReply, ChatModel, Turn
from app.agent.messages import Message
from app.agent.prompt import build_system_prompt
from app.tools.actions import Action, ActionCard

log = logging.getLogger(__name__)
MODEL_TIMEOUT_S = 30


async def update_card(a: Action) -> None:
    """Refresh the action card on the chat message that shows it, and push it to open chats."""
    if a.messageId is None:
        return
    card = ActionCard.of(a)
    await asyncio.to_thread(messages.set_action_card, a.elderId, a.messageId, card)
    await live.broadcast(a.elderId, {"type": "action_update", "messageId": a.messageId, "action": card.model_dump(mode="json")})


async def narrate(model: ChatModel, elder: dict[str, Any], elder_id: str, event: str, fallback: str) -> Message:
    history = await asyncio.to_thread(messages.recent, elder_id, 12)
    turns = [Turn(role=m.sender, text=m.text) for m in history]
    while turns and turns[0].role == "agent":
        turns.pop(0)
    system = build_system_prompt(elder, [], dt.datetime.now(dt.UTC)) + (
        f"\n\nSomething just happened. Tell them about it in 1-2 short sentences: {event}"
    )
    # Gemini needs the conversation to end on a user turn; the update itself is in the system prompt.
    turns.append(Turn("elder", "(no new message from me — please tell me the update)"))
    spoken: str | None = None
    try:
        reply = await asyncio.wait_for(model.complete(system, turns, AgentReply), MODEL_TIMEOUT_S)
        msg = Message(sender="agent", text=reply.reply, quickReplies=reply.quickReplies)
        spoken = reply.spoken
    except Exception:  # noqa: BLE001 — the event already happened; say it plainly rather than not at all
        log.exception("narration failed for elder %s", elder_id)
        msg = Message(sender="agent", text=fallback)
    voice_push.speak_reply(elder_id, elder, msg.id, msg.text, spoken)
    await asyncio.to_thread(messages.save, elder_id, msg)
    await live.broadcast(elder_id, {"type": "message", "message": msg.model_dump(mode="json")})
    return msg
