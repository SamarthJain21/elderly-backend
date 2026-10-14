"""Family chat (elder ↔ caretakers) in elders/{elderId}/family_messages.

Separate from the agent conversation: every caretaker of the elder can read this thread,
while the agent thread stays full_access-only.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from google.cloud.firestore import Query
from pydantic import BaseModel, Field

from app import firebase


class FamilyMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    sender: Literal["elder", "caretaker"]
    senderUid: str
    senderName: str
    text: str
    # For caretaker → elder messages: one-tap answers shown to the elder.
    quickReplies: list[str] = Field(default_factory=list)
    viaAgent: bool = False  # elder asked the agent to pass it on
    createdAt: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))


def _col(elder_id: str):  # noqa: ANN202
    return firebase.get_db().collection("elders").document(elder_id).collection("family_messages")


def save(elder_id: str, msg: FamilyMessage) -> None:
    _col(elder_id).document(msg.id).set(msg.model_dump())


def recent(elder_id: str, limit: int) -> list[FamilyMessage]:
    docs = _col(elder_id).order_by("createdAt", direction=Query.DESCENDING).limit(limit).stream()
    return list(reversed([FamilyMessage(**(d.to_dict() or {})) for d in docs]))


def caretaker_name(uid: str) -> str:
    doc = firebase.get_db().collection("caretakers").document(uid).get().to_dict() or {}
    return str(doc.get("name") or "Family")


# Prototype presets → the elder's one-tap answers (English; other languages ask the model).
PRESET_REPLIES: dict[str, list[str]] = {
    "did you take your medicine?": ["✅ Yes, I took it", "❌ Not yet", "⏰ Remind me later"],
    "how are you feeling today?": ["😊 I'm good", "😐 Okay", "😟 Not great"],
    "shall i order dinner?": ["🍲 Yes please", "🙅 No thanks", "🍞 Something light"],
}
FALLBACK_REPLIES = ["👍 Okay", "❤️ Love you", "📞 Call me"]
