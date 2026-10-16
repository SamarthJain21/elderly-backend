"""Drift detection + weekly summary from daily check-ins. Pure functions — no I/O.

Spec: 7-day rolling baseline, flag on 2+ consecutive deviations. Each signal is compared with THIS elder's own
recent normal, not a fixed threshold: someone who usually feels "okay" isn't flagged for feeling "okay".
"""
from __future__ import annotations

import datetime as dt
from statistics import mean
from typing import Literal

from pydantic import BaseModel

from app.checkin import CheckIn

Signal = Literal["mood", "sleep", "medication", "checkins"]
Status = Literal["stable", "watch", "concern"]

MOOD_SCORE = {"great": 5, "good": 4, "okay": 3, "low": 2, "bad": 1}
SLEEP_SCORE = {"well": 3, "okay": 2, "poorly": 1}
BASELINE_DAYS = 7
MIN_BASELINE = 3          # need at least this many baseline days before judging a signal
CONSECUTIVE = 2           # flag after this many deviating days in a row


class Drift(BaseModel):
    signal: Signal
    days: int                 # consecutive recent days off-baseline
    since: dt.date
    detail: str               # plain words for the family, e.g. "mood lower than usual for 3 days"


class Week(BaseModel):
    days: int
    checkinsDone: int
    moodAvg: float | None     # 1 (bad) … 5 (great)
    moodTrend: Literal["up", "down", "flat"] | None
    medsTakenPct: int | None
    sleptWellPct: int | None
    concerns: list[str]


class Insights(BaseModel):
    status: Status
    drifts: list[Drift]
    week: Week
    summary: str | None = None   # filled by the model (cached), never by guessing in code


def _series(by_day: dict[dt.date, CheckIn], days: list[dt.date], signal: Signal) -> list[float | None]:
    out: list[float | None] = []
    for d in days:
        c = by_day.get(d)
        if signal == "checkins":
            out.append(1.0 if c is not None and c.status == "done" else 0.0)
        elif c is None:
            out.append(None)
        elif signal == "mood":
            out.append(MOOD_SCORE.get(c.mood) if c.mood else None)
        elif signal == "sleep":
            out.append(SLEEP_SCORE.get(c.sleepQuality) if c.sleepQuality else None)
        else:  # medication
            out.append(None if not c.needsMedication or c.medicationTaken is None else (1.0 if c.medicationTaken else 0.0))
    return out


def _is_off(value: float, baseline: float, signal: Signal) -> bool:
    if signal == "mood":
        return value <= baseline - 1 or value <= 2   # a full step below normal, or low/bad outright
    if signal == "sleep":
        return value <= baseline - 1 or value == 1
    return value < 0.5 and baseline >= 0.5           # medication / checkins: missed when usually done


_WORDS = {
    "mood": "mood lower than usual",
    "sleep": "sleeping worse than usual",
    "medication": "medicines missed",
    "checkins": "daily check-in missed",
}


def _off_at(values: list[float | None], i: int, signal: str) -> bool:
    base_vals = [x for x in values[max(0, i - BASELINE_DAYS):i] if x is not None]
    v = values[i]
    return v is not None and len(base_vals) >= MIN_BASELINE and _is_off(v, mean(base_vals), signal)  # type: ignore[arg-type]


def detect_drift(checkins: list[CheckIn], today: dt.date) -> list[Drift]:
    """Signals whose most recent days deviate from the previous 7 days' baseline for CONSECUTIVE+ days in a row.

    Judged on complete days up to yesterday. Today can only extend a run (another off day) — one good answer today
    isn't a trend, so it doesn't clear a drift until tomorrow (and a not-yet-done check-in never counts as missed)."""
    by_day = {c.date: c for c in checkins}
    today_done = (c := by_day.get(today)) is not None and c.status == "done"
    end = today if today_done else today - dt.timedelta(days=1)
    window = [end - dt.timedelta(days=i) for i in range(BASELINE_DAYS + 6, -1, -1)]  # oldest → newest
    drifts: list[Drift] = []
    for signal in ("mood", "sleep", "medication", "checkins"):
        values = _series(by_day, window, signal)  # type: ignore[arg-type]
        run = 0
        for i in range(len(window) - 1, -1, -1):  # walk back from the newest day
            off = _off_at(values, i, signal)
            if window[i] == today and not off:
                continue  # today looks fine (or incomplete): judge the trend on the days before it
            if not off:
                break
            run += 1
        if run >= CONSECUTIVE:
            # The run's oldest day: count back over the days that were off (today may have been skipped).
            newest = len(window) - 1 - (1 if window[-1] == today and not _off_at(values, len(window) - 1, signal) else 0)
            since = window[newest - run + 1]
            drifts.append(Drift(signal=signal, days=run, since=since, detail=f"{_WORDS[signal]} for {run} days"))  # type: ignore[arg-type]
    return drifts


def week_stats(checkins: list[CheckIn], today: dt.date) -> Week:
    days = [today - dt.timedelta(days=i) for i in range(6, -1, -1)]
    week = [c for c in checkins if c.date in days and c.status == "done"]
    prev = [c for c in checkins if today - dt.timedelta(days=13) <= c.date < today - dt.timedelta(days=6) and c.status == "done"]
    moods = [MOOD_SCORE[c.mood] for c in week if c.mood]
    prev_moods = [MOOD_SCORE[c.mood] for c in prev if c.mood]
    meds = [c.medicationTaken for c in week if c.needsMedication and c.medicationTaken is not None]
    sleeps = [c.sleepQuality for c in week if c.sleepQuality]
    trend = None
    if moods and prev_moods:
        delta = mean(moods) - mean(prev_moods)
        trend = "up" if delta >= 0.5 else "down" if delta <= -0.5 else "flat"
    concerns: list[str] = []
    for c in sorted(week, key=lambda c: c.date):
        concerns += [a for a in c.anomalies if a not in concerns]
    return Week(
        days=7, checkinsDone=len(week),
        moodAvg=round(mean(moods), 1) if moods else None, moodTrend=trend,
        medsTakenPct=round(100 * sum(meds) / len(meds)) if meds else None,
        sleptWellPct=round(100 * sum(s == "well" for s in sleeps) / len(sleeps)) if sleeps else None,
        concerns=concerns[:6],
    )


def assess_insights(checkins: list[CheckIn], today: dt.date) -> Insights:
    drifts = detect_drift(checkins, today)
    serious = any(d.signal in ("medication", "checkins") or d.days >= 3 for d in drifts)
    status: Status = "concern" if serious and len(drifts) >= 2 else "watch" if drifts else "stable"
    if any(d.signal == "mood" and d.days >= 3 for d in drifts) and any(d.signal in ("medication", "sleep") for d in drifts):
        status = "concern"
    return Insights(status=status, drifts=drifts, week=week_stats(checkins, today))
