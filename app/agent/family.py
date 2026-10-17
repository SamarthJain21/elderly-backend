"""Family chat (elder ↔ caretakers) in elders/{elderId}/family_messages.

Separate from the agent conversation: every caretaker of the elder can read this thread,
while the agent thread stays full_access-only.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from google.cloud import firestore
from google.cloud.firestore import ArrayUnion, Query
from pydantic import BaseModel, Field, model_validator

from app import firebase
from app.agent.chips import Chip
from app.rx_scan import DraftMedicine


class FamilyMessage(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    # "system" = a note about something that happened ("⏰ Reminder set for 10:30").
    sender: Literal["elder", "caretaker", "system"]
    senderUid: str
    senderName: str
    text: str
    # One-tap chips for whoever RECEIVES this message (elder for caretaker messages, and vice versa).
    chips: list[Chip] = Field(default_factory=list)
    usedChips: list[int] = Field(default_factory=list)  # action chips already carried out
    quickReplies: list[str] = Field(default_factory=list)  # legacy (pre-chips) documents only
    viaAgent: bool = False  # elder asked the agent to pass it on
    # The same message in the receiver's language (family chat across languages); None = same language.
    translated: str | None = None
    translatedTo: str | None = None
    # A photo (app/agent/photos.py): its id, a one-line description in the reader's language (read aloud), and — for
    # an elder's prescription photo — the medicines read from it, for the family to review.
    photoId: str | None = None
    photoAlt: str | None = None
    rx: list[DraftMedicine] | None = None
    createdAt: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @model_validator(mode="after")
    def _legacy_quick_replies(self) -> FamilyMessage:
        if not self.chips and self.quickReplies:
            self.chips = [Chip(label=q) for q in self.quickReplies]
        return self


def _col(elder_id: str):  # noqa: ANN202
    return firebase.get_db().collection("elders").document(elder_id).collection("family_messages")


def save(elder_id: str, msg: FamilyMessage) -> None:
    doc = msg.model_dump()
    # Chips may hold a datetime.date (a remind-on-Sunday chip), which Firestore can't store; keep createdAt a timestamp.
    doc["chips"] = [c.model_dump(mode="json") for c in msg.chips]
    _col(elder_id).document(msg.id).set(doc)


def set_rx(elder_id: str, msg_id: str, medicines: list[dict]) -> None:  # type: ignore[type-arg]
    _col(elder_id).document(msg_id).update({"rx": medicines})


def get(elder_id: str, msg_id: str) -> FamilyMessage | None:
    snap = _col(elder_id).document(msg_id).get()
    return FamilyMessage(**snap.to_dict()) if snap.exists else None


def mark_used(elder_id: str, msg_id: str, chip_index: int) -> bool:
    """Atomically claim an action chip. False if someone already used it (double tap, two caretakers)."""
    db = firebase.get_db()
    ref = _col(elder_id).document(msg_id)

    @firestore.transactional
    def claim(tx: firestore.Transaction) -> bool:
        snap = ref.get(transaction=tx)
        if not snap.exists or chip_index in (snap.to_dict() or {}).get("usedChips", []):
            return False
        tx.update(ref, {"usedChips": ArrayUnion([chip_index])})
        return True

    return claim(db.transaction())


def recent(elder_id: str, limit: int) -> list[FamilyMessage]:
    docs = _col(elder_id).order_by("createdAt", direction=Query.DESCENDING).limit(limit).stream()
    return list(reversed([FamilyMessage(**(d.to_dict() or {})) for d in docs]))


def caretaker_name(uid: str) -> str:
    doc = firebase.get_db().collection("caretakers").document(uid).get().to_dict() or {}
    return str(doc.get("name") or "Family")

