"""POST /elders/{id}/ask — the family asks about their person in their own words ("Has Mum been sleeping well?").

Gemini answers from the last two weeks of check-ins and the helper's activity log only: short, with the days it
saw, and "I don't know — ask her" when the records don't say. No diagnosis or medical advice.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from fastapi import APIRouter, HTTPException, status
from google.genai import types
from pydantic import BaseModel, Field

from app import checkin_store, ratelimit, usage
from app.access import load_elder
from app.agent.llm import genai_client
from app.auth import CurrentUser
from app.config import get_settings
from app.schedule import local_today

log = logging.getLogger(__name__)
router = APIRouter(tags=["insights"])
DAYS = 14

_PROMPT = (
    "A family member asks about {first}, an older adult they care for. Answer ONLY from the records below: daily "
    "check-ins (mood, sleep, meals, medicines, anything mentioned) and what the helper app did. Today is {today} "
    "({weekday}). Read EVERY day before answering, and give the pattern across them with the exceptions by day "
    "(\"slept well most nights, poorly on Friday\"). 1–3 short sentences, plain and warm. If the records don't cover "
    "it, say so plainly and suggest asking {first}. Never diagnose or give medical advice; for a health worry, say "
    "their doctor can check. Answer in the language of the question; the asker's gender is unknown, so avoid "
    "gendered forms for them.\n\n{records}"
)


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=200)


class AskAnswer(BaseModel):
    answer: str
    days: int  # how many days of check-ins it looked at


def _day(c: checkin_store.CheckIn) -> str:
    if c.status != "done":
        return f"{c.date:%a %d %b}: check-in not finished"
    took = {True: "medicines taken", False: "medicines MISSED", None: "medicines not asked"}[c.medicationTaken]
    sleep = f"slept {c.sleepQuality}" + (f" ({c.sleepHours:g} h)" if c.sleepHours else "") if c.sleepQuality else "sleep not asked"
    said = f"; mentioned: {', '.join(c.anomalies)}" if c.anomalies else ""
    return f"{c.date:%a %d %b}: mood {c.mood or '?'}; {sleep}; ate {c.meals or '?'}; {took}{said}"


def records(checkins: list[checkin_store.CheckIn], logs: list[checkin_store.LogEntry], meds: list[str]) -> str:
    """One line per day — only what the family can already see on the check-in and activity screens, no transcripts."""
    days = "\n".join(_day(c) for c in sorted(checkins, key=lambda c: c.date)) or "(no check-ins yet)"
    acts = "\n".join(f"{e.timestamp:%a %d %b %H:%M} UTC: {e.message}" for e in logs) or "(nothing yet)"
    return f"Medicines on the schedule: {', '.join(meds) or 'none'}\n\nDaily check-ins (oldest first):\n{days}\n\nHelper activity (newest first):\n{acts}"


def _ask(question: str, first: str, today: dt.date, recs: str) -> str:
    model = get_settings().gemini_model
    r = genai_client().models.generate_content(
        model=model, contents=question,
        config=types.GenerateContentConfig(temperature=0.2, system_instruction=_PROMPT.format(
            first=first, today=today.isoformat(), weekday=f"{today:%A}", records=recs)),
    )
    usage.gemini(model, r, "family-ask")
    return (r.text or "").strip()


@router.post("/elders/{elder_id}/ask")
async def ask(elder_id: str, body: AskRequest, user: CurrentUser) -> AskAnswer:
    elder = await asyncio.to_thread(load_elder, user, elder_id, caretaker_only=True)
    first = (elder.get("name") or "them").split()[0]
    if elder.get("sharingTier") == "alerts_only":
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"{first} shares alerts only, so there's no day-to-day picture to ask about")
    ratelimit.ask_calls.check(user.uid)
    today = local_today(elder.get("timezone") or "Asia/Kolkata")
    days = [today - dt.timedelta(days=i) for i in range(DAYS)]
    checkins, logs = await asyncio.gather(
        asyncio.to_thread(checkin_store.recent_checkins, elder_id, days),
        asyncio.to_thread(checkin_store.recent_logs, elder_id, 40))
    meds = [f"{m.get('name', '')} {m.get('dose', '')}".strip() for m in elder.get("medicationSchedule") or []]
    try:
        answer = await asyncio.to_thread(_ask, body.question.strip(), first, today, records(checkins, logs, meds))
    except Exception:  # noqa: BLE001 — the check-in screen still has the numbers
        log.warning("family ask failed for elder %s", elder_id, exc_info=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't answer right now — try again") from None
    if not answer:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't answer right now — try again")
    return AskAnswer(answer=answer, days=sum(1 for c in checkins if c.status == "done"))
