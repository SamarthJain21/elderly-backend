"""Emergency alerts (elders/{elderId}/alerts): the SOS button and agent-detected emergencies share one path."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any, Literal

from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel, Field

from app import checkin_store, contacts, firebase
from app.agent import live
from app.checkin_store import LogEntry
from app.sms import send_sms

AlertStatus = Literal["active", "acknowledged", "resolved"]
EMERGENCY_NUMBER = {"Asia/Kolkata": "112", "Asia/Bangkok": "1669", "Asia/Singapore": "995"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Alert(BaseModel):
    id: str = Field(default_factory=lambda: f"alert_{uuid.uuid4().hex[:12]}")
    elderId: str
    source: Literal["sos_button", "agent"]
    description: str = ""
    status: AlertStatus = "active"
    notified: list[str] = Field(default_factory=list)  # names texted (or logged when SMS isn't set up)
    acknowledgedBy: str | None = None
    acknowledgedByName: str | None = None
    resolvedBy: Literal["elder", "caretaker"] | None = None
    createdAt: dt.datetime = Field(default_factory=_now)
    updatedAt: dt.datetime = Field(default_factory=_now)


def _col(elder_id: str):  # noqa: ANN202
    return firebase.get_db().collection("elders").document(elder_id).collection("alerts")


def save(a: Alert) -> None:
    a.updatedAt = _now()
    _col(a.elderId).document(a.id).set(a.model_dump())


def get(elder_id: str, alert_id: str) -> Alert | None:
    snap = _col(elder_id).document(alert_id).get()
    return Alert(**snap.to_dict()) if snap.exists else None


def open_alerts(elder_id: str) -> list[Alert]:
    """Active or acknowledged (still being handled), newest first."""
    docs = _col(elder_id).where(filter=FieldFilter("status", "in", ["active", "acknowledged"])).stream()
    return sorted((Alert(**(d.to_dict() or {})) for d in docs), key=lambda a: a.createdAt, reverse=True)


def who_to_text(elder: dict[str, Any], people: list[contacts.Contact]) -> list[tuple[str, str]]:
    """Emergency contacts first, then trusted family/neighbours from Contacts. Deduped by number, max 5."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    candidates = [(c.get("name", ""), c.get("phone", "")) for c in elder.get("emergencyContacts") or []]
    candidates += [(p.name, p.phone) for p in people if p.trusted and p.kind in ("family", "neighbour", "friend")]
    for name, phone in candidates:
        key = "".join(ch for ch in phone if ch.isdigit())
        if key and key not in seen:
            seen.add(key)
            out.append((name, phone))
    return out[:5]


async def raise_alert(elder_id: str, elder: dict[str, Any], source: Literal["sos_button", "agent"], description: str) -> Alert:
    """Record, tell every open caretaker screen, text emergency contacts. Never waits on approval."""
    a = Alert(elderId=elder_id, source=source, description=description[:300])
    name = elder.get("name", "Your family member")
    people = await asyncio.to_thread(contacts.list_contacts, elder_id)
    address = elder.get("address") or {}
    where = ", ".join(x for x in (address.get("line1"), address.get("landmark"), address.get("city")) if x)
    what = "pressed the SOS button" if source == "sos_button" else f"may need urgent help: {description}"
    body = (f"🚨 Elderly alert: {name} {what}. {('Address: ' + where + '. ') if where else ''}"
            f"Please call them now on {elder.get('phone', '')}. Emergency number: {EMERGENCY_NUMBER.get(elder.get('timezone') or '', '112')}.")
    for person, phone in who_to_text(elder, people):
        via = await asyncio.to_thread(send_sms, phone, body)
        a.notified.append(person if via == "sms" else f"{person} (text not sent — SMS not set up)")
    await asyncio.to_thread(save, a)
    await asyncio.to_thread(checkin_store.add_log, elder_id, LogEntry(
        message=f"🚨 SOS — {name} {what}" + (f". Texted: {', '.join(a.notified)}" if a.notified else ""),
        kind="alert", actor="elder" if source == "sos_button" else "agent"))
    await live.broadcast(elder_id, {"type": "alert", "alert": a.model_dump(mode="json")}, "family")
    return a
