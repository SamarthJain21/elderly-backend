"""Agent actions in elders/{elderId}/actions. An action's status is the approval state —
there's no separate approvals collection to keep in sync."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, Field

from app import firebase
from app.autonomy import format_money
from app.tools.providers import Quote

ActionStatus = Literal["awaiting_elder", "awaiting_caretaker", "done", "declined", "cancelled", "blocked", "failed"]
PENDING: frozenset[str] = frozenset({"awaiting_elder", "awaiting_caretaker"})

TOOL_TITLES = {
    "order_food": "Food order", "order_medicine": "Medicine order", "book_ride": "Ride",
    "set_reminder": "Reminder", "send_message_to_caretaker": "Message to family",
}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Action(BaseModel):
    id: str = Field(default_factory=lambda: f"act_{uuid.uuid4().hex[:16]}")
    elderId: str
    tool: str
    args: dict[str, Any]
    status: ActionStatus
    quote: Quote | None = None
    orderId: str | None = None
    requestText: str = ""         # what the elder said that led to this
    messageId: str | None = None  # chat message showing this action's card
    decidedBy: str | None = None  # uid of whoever approved/declined
    createdAt: dt.datetime = Field(default_factory=_now)
    updatedAt: dt.datetime = Field(default_factory=_now)

    def summary(self) -> str:
        title = TOOL_TITLES.get(self.tool, self.tool)
        if self.quote is not None:
            return f"{title}: {', '.join(self.quote.lines)} from {self.quote.provider}, " \
                   f"{format_money(self.quote.amountMinor, self.quote.currency)}"
        detail = self.args.get("title") or self.args.get("message") or ""
        return f"{title}: {detail}" if detail else title


class ActionCard(BaseModel):
    """Snapshot of an action embedded in a chat message, so the UI renders without another read."""

    id: str
    tool: str
    title: str
    status: ActionStatus
    lines: list[str]
    amount: str | None
    provider: str | None
    eta: str | None
    orderId: str | None

    @classmethod
    def of(cls, a: Action) -> ActionCard:
        q = a.quote
        lines = q.lines if q else [str(a.args.get("title") or a.args.get("message") or "")]
        return cls(
            id=a.id, tool=a.tool, title=TOOL_TITLES.get(a.tool, a.tool), status=a.status, lines=[x for x in lines if x],
            amount=format_money(q.amountMinor, q.currency) if q else None,
            provider=q.provider if q else None, eta=q.eta if q else None, orderId=a.orderId,
        )


def _col(elder_id: str):  # noqa: ANN202
    return firebase.get_db().collection("elders").document(elder_id).collection("actions")


def save(a: Action) -> None:
    a.updatedAt = _now()
    _col(a.elderId).document(a.id).set(a.model_dump(mode="json"))


def get(elder_id: str, action_id: str) -> Action | None:
    snap = _col(elder_id).document(action_id).get()
    return Action(**snap.to_dict()) if snap.exists else None


def with_status(elder_id: str, status: ActionStatus) -> list[Action]:
    docs = _col(elder_id).where(filter=FieldFilter("status", "==", status)).stream()
    return sorted((Action(**(d.to_dict() or {})) for d in docs), key=lambda a: a.createdAt)
