from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, HTTPException, status
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import BaseModel

from app import firebase
from app.access import load_elder
from app.auth import CurrentUser
from app.schedule import Reminder, ReminderIn, local_today, reminders_for_day

router = APIRouter(prefix="/elders/{elder_id}/reminders", tags=["reminders"])


def all_reminders(elder_id: str) -> list[Reminder]:
    docs = (
        firebase.get_db().collection("reminders")
        .where(filter=FieldFilter("elderId", "==", elder_id))
        .stream()
    )
    return [Reminder(**(d.to_dict() or {})) for d in docs]


@router.get("")
def list_reminders(elder_id: str, user: CurrentUser) -> list[Reminder]:
    load_elder(user, elder_id)
    return sorted(all_reminders(elder_id), key=lambda r: (r.kind != "medication", r.time))


class TodayReminders(BaseModel):
    date: dt.date
    timezone: str
    reminders: list[Reminder]


@router.get("/today")
def todays_reminders(elder_id: str, user: CurrentUser) -> TodayReminders:
    elder = load_elder(user, elder_id)
    tz = elder.get("timezone")
    if tz is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Assessment not completed")
    today = local_today(tz)
    return TodayReminders(date=today, timezone=tz, reminders=reminders_for_day(all_reminders(elder_id), today))


@router.post("", status_code=status.HTTP_201_CREATED)
def create_reminder(elder_id: str, body: ReminderIn, user: CurrentUser) -> Reminder:
    load_elder(user, elder_id, caretaker_only=True)
    reminder = Reminder(id=f"rem_{uuid.uuid4().hex[:16]}", elderId=elder_id, **body.model_dump())
    firebase.get_db().collection("reminders").document(reminder.id).set(reminder.model_dump(mode="json"))
    return reminder


def save_reminder(elder_id: str, title: str, time: str, day: dt.date) -> Reminder:
    """One-off reminder created by the agent."""
    reminder = Reminder(id=f"rem_{uuid.uuid4().hex[:16]}", elderId=elder_id, title=title, kind="custom", time=time, date=day)
    firebase.get_db().collection("reminders").document(reminder.id).set(reminder.model_dump(mode="json"))
    return reminder


@router.delete("/{reminder_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reminder(elder_id: str, reminder_id: str, user: CurrentUser) -> None:
    load_elder(user, elder_id, caretaker_only=True)
    ref = firebase.get_db().collection("reminders").document(reminder_id)
    snap = ref.get()
    data = snap.to_dict() if snap.exists else None
    if data is None or data.get("elderId") != elder_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reminder not found")
    if data.get("kind") == "medication":
        raise HTTPException(status.HTTP_409_CONFLICT, "Edit the medicine in the assessment instead")
    ref.delete()
