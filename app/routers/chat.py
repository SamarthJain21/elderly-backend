"""Elder <-> agent chat over WebSocket.

Protocol (JSON frames):
  client -> {"type": "auth", "token": "<Firebase ID token>"}      must be first, within 10s
  server -> {"type": "history", "messages": [Message, ...]}
  client -> {"type": "message", "text": "..."}
  server -> {"type": "message", "message": Message}               echo of the saved elder message
  server -> {"type": "typing"}
  server -> {"type": "message", "message": Message}               agent reply with quickReplies
  server -> {"type": "error", "message": "..."}                   recoverable; socket stays open
Close codes: 4401 not authenticated, 4403 no access to this elder.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from app.access import load_elder
from app.agent import messages
from app.agent.llm import ChatModel, Turn, get_chat_model
from app.agent.messages import Message
from app.agent.prompt import build_system_prompt
from app.auth import AuthedUser, authenticate
from app.routers.reminders import all_reminders
from app.schedule import local_today, reminders_for_day

log = logging.getLogger(__name__)
router = APIRouter()

AUTH_TIMEOUT_S = 10
MODEL_TIMEOUT_S = 45
HISTORY_TURNS = 20
HISTORY_ON_CONNECT = 50


class ClientMessage(BaseModel):
    type: str
    text: str = Field(min_length=1, max_length=1000)


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


def _build_turns(history: list[Message]) -> list[Turn]:
    turns = [Turn(role=m.sender, text=m.text) for m in history]
    # Gemini expects the conversation to open with a user turn.
    while turns and turns[0].role == "agent":
        turns.pop(0)
    return turns


async def _agent_reply(model: ChatModel, user: AuthedUser, elder_id: str) -> Message:
    elder = await asyncio.to_thread(load_elder, user, elder_id)  # fresh: assessment may have changed
    now = dt.datetime.now(dt.UTC)
    today = []
    if tz := elder.get("timezone"):
        everything = await asyncio.to_thread(all_reminders, elder_id)
        today = reminders_for_day(everything, local_today(tz, now))
    history = await asyncio.to_thread(messages.recent, elder_id, HISTORY_TURNS)
    reply = await asyncio.wait_for(
        model.reply(build_system_prompt(elder, today, now), _build_turns(history)), MODEL_TIMEOUT_S
    )
    return Message(sender="agent", text=reply.reply, quickReplies=reply.quickReplies)


@router.websocket("/ws/{elder_id}")
async def chat(ws: WebSocket, elder_id: str, model: Annotated[ChatModel, Depends(get_chat_model)]) -> None:
    await ws.accept()
    user = await _authenticate(ws, elder_id)
    if user is None:
        return

    history = await asyncio.to_thread(messages.recent, elder_id, HISTORY_ON_CONNECT)
    await ws.send_json({"type": "history", "messages": [m.model_dump(mode="json") for m in history]})

    try:
        while True:
            raw = await ws.receive_json()
            try:
                incoming = ClientMessage.model_validate(raw)
            except ValidationError:
                await ws.send_json({"type": "error", "message": "Message must be 1–1000 characters"})
                continue
            if incoming.type != "message":
                continue

            elder_msg = Message(
                sender="elder", text=incoming.text.strip(),
                actedBy=user.uid if user.role == "caretaker" else None,
            )
            await asyncio.to_thread(messages.save, elder_id, elder_msg)
            await ws.send_json({"type": "message", "message": elder_msg.model_dump(mode="json")})
            await ws.send_json({"type": "typing"})

            try:
                agent_msg = await _agent_reply(model, user, elder_id)
            except Exception:
                log.exception("agent reply failed for elder %s", elder_id)
                await ws.send_json({"type": "error", "message": "Sorry, I couldn't answer just now. Please try again."})
                continue
            await asyncio.to_thread(messages.save, elder_id, agent_msg)
            await ws.send_json({"type": "message", "message": agent_msg.model_dump(mode="json")})
    except WebSocketDisconnect:
        return
