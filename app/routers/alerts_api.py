"""SOS button and alert handling."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app import alerts, firebase
from app.access import load_elder
from app.agent import family, live
from app.alerts import EMERGENCY_NUMBER, Alert
from app.auth import CurrentUser

router = APIRouter(tags=["alerts"])


class SosResult(BaseModel):
    alert: Alert
    emergencyNumber: str
    firstContact: dict[str, str] | None  # {name, phone} for the elder's "Call …" button


@router.post("/elders/{elder_id}/sos", status_code=status.HTTP_201_CREATED)
async def press_sos(elder_id: str, user: CurrentUser) -> SosResult:
    """The elder held the SOS bar and didn't cancel the 10-second countdown."""
    elder = await asyncio.to_thread(load_elder, user, elder_id)
    if not (user.role == "elder" and user.uid == elder_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only the elder can press SOS")
    open_now = await asyncio.to_thread(alerts.open_alerts, elder_id)
    a = open_now[0] if open_now else await alerts.raise_alert(elder_id, elder, "sos_button", "SOS button")
    contacts_ = elder.get("emergencyContacts") or []
    first = {"name": contacts_[0]["name"], "phone": contacts_[0]["phone"]} if contacts_ else None
    return SosResult(alert=a, emergencyNumber=EMERGENCY_NUMBER.get(elder.get("timezone") or "", "112"), firstContact=first)


@router.get("/elders/{elder_id}/alerts")
def list_open_alerts(elder_id: str, user: CurrentUser) -> list[Alert]:
    load_elder(user, elder_id)
    return alerts.open_alerts(elder_id)


class OpenAlert(BaseModel):
    alert: Alert
    elderName: str
    elderPhone: str


@router.get("/alerts")
def my_open_alerts(user: CurrentUser) -> list[OpenAlert]:
    """Every open alert across the caretaker's elders — one call for the dashboard banner."""
    if user.role != "caretaker":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Caretakers only")
    me = firebase.get_db().collection("caretakers").document(user.uid).get().to_dict() or {}
    out: list[OpenAlert] = []
    for elder_id in me.get("elderIds", []):
        try:
            elder = load_elder(user, elder_id)
        except HTTPException:
            continue
        out += [OpenAlert(alert=a, elderName=elder.get("name", ""), elderPhone=elder.get("phone", ""))
                for a in alerts.open_alerts(elder_id)]
    return sorted(out, key=lambda o: o.alert.createdAt, reverse=True)


class AlertUpdate(BaseModel):
    action: str = Field(pattern="^(acknowledge|resolve)$")


@router.post("/elders/{elder_id}/alerts/{alert_id}")
async def update_alert(elder_id: str, alert_id: str, body: AlertUpdate, user: CurrentUser) -> Alert:
    """Caretaker: acknowledge ("I'm handling it") or resolve. Elder: resolve ("I'm okay now")."""
    await asyncio.to_thread(load_elder, user, elder_id)
    a = await asyncio.to_thread(alerts.get, elder_id, alert_id)
    if a is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Alert not found")
    if a.status == "resolved":
        return a
    if body.action == "acknowledge":
        if user.role != "caretaker":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only family can acknowledge")
        a.status, a.acknowledgedBy = "acknowledged", user.uid
        a.acknowledgedByName = await asyncio.to_thread(family.caretaker_name, user.uid)
    else:
        a.status, a.resolvedBy = "resolved", "elder" if user.role == "elder" else "caretaker"
    await asyncio.to_thread(alerts.save, a)
    await live.broadcast(elder_id, {"type": "alert", "alert": a.model_dump(mode="json")}, "family")
    return a
