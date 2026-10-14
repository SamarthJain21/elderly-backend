"""Chat messages in elders/{elderId}/messages (subcollection: ordering by createdAt needs no composite index)."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from google.cloud.firestore import Query
from pydantic import BaseModel, Field

from app import firebase

Sender = Literal["elder", "agent"]


class Message(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    sender: Sender
    text: str
    quickReplies: list[str] = Field(default_factory=list)
    createdAt: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    # Set when a caretaker typed this in "Act as Elder" mode.
    actedBy: str | None = None


def _col(elder_id: str):  # noqa: ANN202 — firestore CollectionReference
    return firebase.get_db().collection("elders").document(elder_id).collection("messages")


def save(elder_id: str, msg: Message) -> None:
    _col(elder_id).document(msg.id).set(msg.model_dump())


def recent(elder_id: str, limit: int) -> list[Message]:
    """Newest `limit` messages, returned oldest-first."""
    docs = _col(elder_id).order_by("createdAt", direction=Query.DESCENDING).limit(limit).stream()
    return list(reversed([Message(**(d.to_dict() or {})) for d in docs]))
