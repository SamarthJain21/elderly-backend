"""Caretaker insights: drift flags + this week's numbers + a short model-written summary (cached per day)."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app import checkin_store, firebase
from app.access import load_elder
from app.agent.llm import AgentReply, ChatModel, Turn, get_chat_model
from app.auth import CurrentUser
from app.insights import Insights, assess_insights
from app.schedule import local_today

log = logging.getLogger(__name__)
router = APIRouter(tags=["insights"])
HISTORY_DAYS = 14


def load_insights(elder_id: str, elder: dict[str, Any]) -> Insights:
    tz = elder.get("timezone") or "Asia/Kolkata"
    today = local_today(tz)
    days = [today - dt.timedelta(days=i) for i in range(HISTORY_DAYS)]
    return assess_insights(checkin_store.recent_checkins(elder_id, days), today)


def _summary_doc(elder_id: str, day: dt.date):  # noqa: ANN202
    return firebase.get_db().collection("elders").document(elder_id).collection("insights").document(day.isoformat())


async def _summary(model: ChatModel, elder: dict[str, Any], elder_id: str, ins: Insights, today: dt.date) -> str | None:
    """2–3 sentences for the family, written from the computed numbers only. Cached until the numbers change."""
    facts = ins.model_dump(mode="json", exclude={"summary"})
    key = hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()[:16]
    ref = _summary_doc(elder_id, today)
    cached = (await asyncio.to_thread(ref.get)).to_dict() or {}
    if cached.get("key") == key:
        return cached.get("summary")
    first = (elder.get("name") or "They").split()[0]
    system = (
        f"You write a short weekly note for the family of {first}, an older adult, from their daily check-ins. "
        "Use ONLY the facts given (mood is 1=bad … 5=great; percentages are of days with an answer). "
        "2-3 warm, plain sentences in English: what went well, anything to keep an eye on, and one gentle, "
        "practical suggestion if something is off (e.g. a call, a doctor visit if concerns persist). "
        "No diagnosis, no alarmism, no invented details. Put the note in `reply`; quickReplies empty.\n\n"
        f"Facts: {json.dumps(facts)}"
    )
    try:
        out = await asyncio.wait_for(model.complete(system, [Turn("elder", "(write the weekly note)")], AgentReply), 20)
    except Exception:  # noqa: BLE001 — the numbers stand on their own
        log.warning("insights summary failed for elder %s", elder_id)
        return None
    await asyncio.to_thread(ref.set, {"key": key, "summary": out.reply, "createdAt": dt.datetime.now(dt.UTC)})
    return out.reply


@router.get("/elders/{elder_id}/insights")
async def get_insights(elder_id: str, user: CurrentUser, model: Annotated[ChatModel, Depends(get_chat_model)]) -> Insights:
    elder = await asyncio.to_thread(load_elder, user, elder_id, caretaker_only=True)
    ins = await asyncio.to_thread(load_insights, elder_id, elder)
    tier = elder.get("sharingTier", "daily_summary")
    if tier == "alerts_only":
        # Only that something needs attention — not the day-by-day picture.
        ins.week = ins.week.model_copy(update={"moodAvg": None, "moodTrend": None, "medsTakenPct": None,
                                               "sleptWellPct": None, "concerns": []})
        ins.drifts = [d.model_copy(update={"detail": "something has changed — check in with them"}) for d in ins.drifts]
        return ins
    if ins.week.checkinsDone == 0:
        return ins
    ins.summary = await _summary(model, elder, elder_id, ins, local_today(elder.get("timezone") or "Asia/Kolkata"))
    return ins


def _flags_doc(elder_id: str):  # noqa: ANN202
    return firebase.get_db().collection("elders").document(elder_id).collection("insights").document("flags")


def new_drift_flags(elder_id: str, elder: dict[str, Any]) -> list[str]:
    """After a check-in: log each NEW drift once per signal per day. Returns the messages logged."""
    ins = load_insights(elder_id, elder)
    if not ins.drifts:
        return []
    today = local_today(elder.get("timezone") or "Asia/Kolkata").isoformat()
    ref = _flags_doc(elder_id)
    flagged: dict[str, str] = (ref.get().to_dict() or {})
    name = (elder.get("name") or "They").split()[0]
    logged: list[str] = []
    for d in ins.drifts:
        if flagged.get(d.signal) == today:
            continue
        msg = f"📉 {name}: {d.detail} (since {d.since:%a %d %b}). Worth a call."
        checkin_store.add_log(elder_id, checkin_store.LogEntry(message=msg, kind="warn", actor="system"))
        flagged[d.signal] = today
        logged.append(msg)
    if logged:
        ref.set(flagged)
    return logged

