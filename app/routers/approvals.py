"""Caretaker approvals: actions the agent proposed that wait on the family."""
from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from google.cloud.firestore import Query as FsQuery
from pydantic import BaseModel

from app import checkin_store, firebase
from app.access import load_elder
from app.agent.llm import ChatModel, get_chat_model
from app.agent.narrate import narrate, update_card
from app.auth import CurrentUser
from app.checkin_store import LogEntry
from app.tools import actions
from app.tools.actions import Action
from app.tools.executor import perform

router = APIRouter(prefix="/elders/{elder_id}", tags=["approvals"])


@router.get("/approvals")
def pending_approvals(elder_id: str, user: CurrentUser) -> list[Action]:
    load_elder(user, elder_id, caretaker_only=True)
    return actions.with_status(elder_id, "awaiting_caretaker")


@router.get("/actions")
def recent_actions(elder_id: str, user: CurrentUser, limit: int = Query(20, ge=1, le=100)) -> list[Action]:
    load_elder(user, elder_id)
    docs = (
        firebase.get_db().collection("elders").document(elder_id).collection("actions")
        .order_by("createdAt", direction=FsQuery.DESCENDING).limit(limit).stream()
    )
    return [Action(**(d.to_dict() or {})) for d in docs]


class Decision(BaseModel):
    approve: bool


@router.post("/actions/{action_id}/decide")
async def decide_action(
    elder_id: str, action_id: str, body: Decision, user: CurrentUser,
    model: Annotated[ChatModel, Depends(get_chat_model)],
) -> Action:
    elder = await asyncio.to_thread(load_elder, user, elder_id, caretaker_only=True)
    a = await asyncio.to_thread(actions.get, elder_id, action_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    if a.status != "awaiting_caretaker":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Already {a.status.replace('_', ' ')}")

    a.decidedBy = user.uid
    if body.approve:
        result = await perform(a, elder)
        event = f"Their family approved: {a.summary()} → {result}"
    else:
        a.status = "declined"
        await asyncio.to_thread(actions.save, a)
        await asyncio.to_thread(checkin_store.add_log, elder_id,
                                LogEntry(message=f"Declined — {a.summary()}", kind="ok", actor="caretaker"))
        result = {"say": "Your family said not this time."}
        event = f"Their family decided not to go ahead with: {a.summary()}. Be gentle; suggest they talk to family."
    await update_card(a)
    await narrate(model, elder, elder_id, event, str(result.get("say", "")))
    return a
