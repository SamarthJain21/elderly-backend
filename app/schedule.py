"""Assessment models and pure scheduling logic (no Firestore here, so it's easy to test)."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ALL_DAYS: list[Day] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _check_hhmm(v: str) -> str:
    try:
        dt.datetime.strptime(v, "%H:%M")
    except ValueError as exc:
        raise ValueError("time must be HH:MM (24h)") from exc
    if len(v) != 5:  # strptime accepts "8:5"; we want "08:05" so string sort == time sort
        raise ValueError("time must be HH:MM (24h)")
    return v


def _check_tz(v: str) -> str:
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone {v!r}") from exc
    return v


HHMM = Annotated[str, AfterValidator(_check_hhmm)]
TimeZone = Annotated[str, AfterValidator(_check_tz)]


def _unique_days(v: list[Day]) -> list[Day]:
    if len(set(v)) != len(v):
        raise ValueError("days must be unique")
    return sorted(v, key=ALL_DAYS.index)


Days = Annotated[list[Day], AfterValidator(_unique_days)]


class Sleep(BaseModel):
    """Either can be unknown: the family may skip it during setup."""
    bedtime: HHMM | None = None
    wakeTime: HHMM | None = None


class Meals(BaseModel):
    breakfast: HHMM | None = None
    lunch: HHMM | None = None
    dinner: HHMM | None = None


class Medication(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = Field(min_length=1, max_length=100)
    dose: str = Field(default="", max_length=60)
    times: list[HHMM] = Field(min_length=1, max_length=8)
    withFood: bool = False
    notes: str = Field(default="", max_length=300)

    @field_validator("times")
    @classmethod
    def _sorted_unique(cls, v: list[str]) -> list[str]:
        return sorted(set(v))


class CheckInSchedule(BaseModel):
    enabled: bool = True  # the family can skip the daily check-in; then no day counts as "missed"
    time: HHMM = "09:00"
    days: Days = Field(default_factory=lambda: list(ALL_DAYS), min_length=1)


class Assessment(BaseModel):
    timezone: TimeZone
    sleep: Sleep
    meals: Meals = Meals()
    medications: list[Medication] = Field(default_factory=list, max_length=30)
    checkIn: CheckInSchedule = CheckInSchedule()
    healthNotes: str = Field(default="", max_length=1000)


ReminderKind = Literal["medication", "appointment", "custom"]


class ReminderIn(BaseModel):
    """Caretaker-created reminder: either recurring (`days`) or one-off (`date`)."""

    title: str = Field(min_length=1, max_length=120)
    kind: Literal["appointment", "custom"] = "custom"
    time: HHMM
    days: Days | None = None
    date: dt.date | None = None

    @model_validator(mode="after")
    def _one_schedule(self) -> ReminderIn:
        if (self.days is None) == (self.date is None):
            raise ValueError("set exactly one of days (recurring) or date (one-off)")
        return self


class Reminder(BaseModel):
    id: str
    elderId: str
    title: str
    kind: ReminderKind
    time: HHMM
    days: list[Day] | None = None
    date: dt.date | None = None
    medicationId: str | None = None
    active: bool = True


def medication_reminders(elder_id: str, meds: list[Medication]) -> list[Reminder]:
    """One daily reminder per medication per time. IDs are deterministic so regenerating is idempotent."""
    out: list[Reminder] = []
    for med in meds:
        title = f"Take {med.name}" + (f" ({med.dose})" if med.dose else "")
        if med.withFood:
            title += " with food"
        for t in med.times:
            out.append(Reminder(
                id=f"med_{elder_id}_{med.id}_{t.replace(':', '')}",
                elderId=elder_id, title=title, kind="medication", time=t,
                days=list(ALL_DAYS), medicationId=med.id,
            ))
    return out


def reminders_for_day(reminders: list[Reminder], day: dt.date) -> list[Reminder]:
    weekday = ALL_DAYS[day.weekday()]
    due = [
        r for r in reminders
        if r.active and ((r.date is not None and r.date == day) or (r.days is not None and weekday in r.days))
    ]
    return sorted(due, key=lambda r: r.time)


def local_today(tz: str, now: dt.datetime | None = None) -> dt.date:
    """The elder's calendar date — not the server's. Cloud Run runs in UTC."""
    now = now or dt.datetime.now(dt.UTC)
    return now.astimezone(ZoneInfo(tz)).date()
