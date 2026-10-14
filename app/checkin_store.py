"""Firestore I/O for check-ins and activity logs."""
from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo
from typing import Any, Literal

from google.cloud.firestore import Query
from pydantic import BaseModel, Field

from app import firebase
from app.checkin import CheckIn, LogKind, Status, checkin_id, status_for
from app.schedule import ALL_DAYS, Day


class LogEntry(BaseModel):
    """elders/{elderId}/logs/{id} — subcollection so newest-first needs no composite index."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    timestamp: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    actor: Literal["agent", "elder", "caretaker", "system"] = "agent"
    message: str
    kind: LogKind
    ack: bool = False
    checkInId: str | None = None


class TodayCheckIn(BaseModel):
    status: Status
    date: dt.date
    time: str
    checkIn: CheckIn | None


def _db():  # noqa: ANN202
    return firebase.get_db()


def get_checkin(cid: str) -> CheckIn | None:
    snap = _db().collection("check_ins").document(cid).get()
    return CheckIn(**snap.to_dict()) if snap.exists else None


def save_checkin(c: CheckIn) -> None:
    _db().collection("check_ins").document(c.id).set(c.model_dump(mode="json"))


def recent_checkins(elder_id: str, days: list[dt.date]) -> list[CheckIn]:
    refs = [_db().collection("check_ins").document(checkin_id(elder_id, d)) for d in days]
    return [CheckIn(**s.to_dict()) for s in _db().get_all(refs) if s.exists]


def save_transcript(c: CheckIn, text: str) -> None:
    _db().collection("check_in_transcripts").document(c.id).set({"elderId": c.elderId, "rawTranscript": text})


def add_log(elder_id: str, entry: LogEntry) -> None:
    _db().collection("elders").document(elder_id).collection("logs").document(entry.id).set(entry.model_dump())


def recent_logs(elder_id: str, limit: int) -> list[LogEntry]:
    docs = (
        _db().collection("elders").document(elder_id).collection("logs")
        .order_by("timestamp", direction=Query.DESCENDING).limit(limit).stream()
    )
    return [LogEntry(**(d.to_dict() or {})) for d in docs]


def schedule_of(elder_id: str) -> tuple[str, list[Day]]:
    rules = _db().collection("rules").document(elder_id).get().to_dict() or {}
    return rules.get("checkInTime", "09:00"), rules.get("checkInDays", list(ALL_DAYS))


def today_checkin(elder_id: str, elder: dict[str, Any], now_utc: dt.datetime) -> TodayCheckIn | None:
    """None if the assessment (timezone) isn't done yet."""
    tz = elder.get("timezone")
    if tz is None:
        return None
    time_hhmm, days = schedule_of(elder_id)
    local_day = now_utc.astimezone(ZoneInfo(tz)).date()
    existing = get_checkin(checkin_id(elder_id, local_day))
    status, day = status_for(time_hhmm=time_hhmm, days=days, tz=tz, now_utc=now_utc, existing=existing)
    return TodayCheckIn(status=status, date=day, time=time_hhmm, checkIn=existing)
