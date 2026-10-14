"""Daily check-in: scheduling status, state merging and escalation. Pure — no I/O."""
from __future__ import annotations

import datetime as dt
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.schedule import ALL_DAYS, Day

Mood = Literal["great", "good", "okay", "low", "bad"]
SleepQuality = Literal["well", "okay", "poorly"]
Field_ = Literal["mood", "sleep", "meals", "medication"]
Status = Literal["not_today", "upcoming", "due", "in_progress", "done", "missed"]
LogKind = Literal["ok", "warn", "alert"]

MISSED_AFTER = dt.timedelta(hours=3)
MAX_TURNS = 8
MOOD_LABEL: dict[Mood, str] = {"great": "great", "good": "good", "okay": "okay", "low": "a bit low", "bad": "not good"}


class CheckIn(BaseModel):
    """check_ins/{elderId}_{YYYY-MM-DD}"""

    id: str
    elderId: str
    date: dt.date
    status: Literal["in_progress", "done"] = "in_progress"
    mood: Mood | None = None
    sleepQuality: SleepQuality | None = None
    sleepHours: float | None = None
    meals: str | None = None
    medicationTaken: bool | None = None
    anomalies: list[str] = Field(default_factory=list)
    urgent: list[str] = Field(default_factory=list)  # subset of anomalies that need the family now
    escalateFlag: bool = False
    turns: int = 0
    needsMedication: bool = True
    startedAt: dt.datetime
    completedAt: dt.datetime | None = None


class Extracted(BaseModel):
    """What the model pulled out of the elder's latest message. None = not mentioned."""

    mood: Mood | None = None
    sleepQuality: SleepQuality | None = None
    sleepHours: float | None = Field(default=None, ge=0, le=24)
    meals: str | None = None
    medicationTaken: bool | None = None
    concerns: list[str] = Field(default_factory=list)
    urgent: list[str] = Field(
        default_factory=list,
        description="Only concerns that are urgent: a fall, chest pain, trouble breathing, bleeding, fainting or "
        "dizziness, confusion, not eating all day, or thoughts of self-harm. Minor aches are NOT urgent.",
    )


def _add_unique(existing: list[str], new: list[str]) -> list[str]:
    return existing + [s for s in (s.strip() for s in new) if s and s not in existing]


def checkin_id(elder_id: str, day: dt.date) -> str:
    return f"{elder_id}_{day.isoformat()}"


def scheduled_at(day: dt.date, time_hhmm: str, tz: str) -> dt.datetime:
    h, m = map(int, time_hhmm.split(":"))
    return dt.datetime(day.year, day.month, day.day, h, m, tzinfo=ZoneInfo(tz))


def status_for(
    *, time_hhmm: str, days: list[Day], tz: str, now_utc: dt.datetime, existing: CheckIn | None
) -> tuple[Status, dt.date]:
    """Today's check-in status in the elder's timezone. Returns (status, local date)."""
    local = now_utc.astimezone(ZoneInfo(tz))
    today = local.date()
    if existing is not None:
        return existing.status, today
    if ALL_DAYS[today.weekday()] not in days:
        return "not_today", today
    due = scheduled_at(today, time_hhmm, tz)
    if local < due:
        return "upcoming", today
    if local >= due + MISSED_AFTER:
        return "missed", today
    return "due", today


def next_missing(c: CheckIn) -> Field_ | None:
    if c.mood is None:
        return "mood"
    if c.sleepQuality is None:
        return "sleep"
    if c.meals is None:
        return "meals"
    if c.needsMedication and c.medicationTaken is None:
        return "medication"
    return None


def merge(c: CheckIn, x: Extracted) -> CheckIn:
    """Fill only fields the model actually extracted; never overwrite with None."""
    updates = {k: v for k, v in x.model_dump(exclude={"concerns", "urgent"}).items() if v is not None}
    anomalies = _add_unique(_add_unique(c.anomalies, x.concerns), x.urgent)  # urgent ones are concerns too
    return c.model_copy(update={**updates, "anomalies": anomalies, "urgent": _add_unique(c.urgent, x.urgent)})


def assess(c: CheckIn) -> tuple[LogKind, bool, str]:
    """Deterministic triage — the model reports facts, code decides how worried to be."""
    alert = c.mood == "bad" or bool(c.urgent)
    warn = c.mood == "low" or c.medicationTaken is False or c.sleepQuality == "poorly" or bool(c.anomalies)
    kind: LogKind = "alert" if alert else "warn" if warn else "ok"

    parts = []
    if c.mood:
        parts.append(f"feeling {MOOD_LABEL[c.mood]}")
    if c.sleepQuality:
        parts.append(f"slept {c.sleepQuality}" + (f" (~{c.sleepHours:g}h)" if c.sleepHours is not None else ""))
    if c.meals:
        parts.append(f"meals: {c.meals}")
    if c.needsMedication and c.medicationTaken is not None:
        parts.append("took medicines" if c.medicationTaken else "has NOT taken medicines")
    if c.anomalies:
        parts.append("concerns: " + "; ".join(c.anomalies))
    missing = next_missing(c)
    summary = "Check-in: " + (", ".join(parts) if parts else "no answers")
    if missing is not None:
        summary += " (incomplete)"
    return kind, alert, summary
