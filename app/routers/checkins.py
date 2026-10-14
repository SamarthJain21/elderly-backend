from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Query, status

from app import checkin_store
from app.agent import family
from app.agent.family import FamilyMessage
from app.access import load_elder
from app.auth import CurrentUser
from app.checkin import CheckIn
from app.checkin_store import LogEntry, TodayCheckIn
from app.schedule import local_today

router = APIRouter(prefix="/elders/{elder_id}", tags=["check-ins"])


@router.get("/checkins/today")
def todays_checkin(elder_id: str, user: CurrentUser) -> TodayCheckIn:
    elder = load_elder(user, elder_id)
    state = checkin_store.today_checkin(elder_id, elder, dt.datetime.now(dt.UTC))
    if state is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Assessment not completed")
    return state


@router.get("/checkins")
def recent_checkins(elder_id: str, user: CurrentUser, days: int = Query(7, ge=1, le=31)) -> list[CheckIn]:
    """Newest first, one per day that has a check-in."""
    elder = load_elder(user, elder_id)
    tz = elder.get("timezone")
    if tz is None:
        return []
    today = local_today(tz)
    found = checkin_store.recent_checkins(elder_id, [today - dt.timedelta(days=i) for i in range(days)])
    return sorted(found, key=lambda c: c.date, reverse=True)


@router.get("/logs")
def recent_logs(elder_id: str, user: CurrentUser, limit: int = Query(20, ge=1, le=100)) -> list[LogEntry]:
    load_elder(user, elder_id, caretaker_only=True)
    return checkin_store.recent_logs(elder_id, limit)


@router.get("/family")
def family_messages(elder_id: str, user: CurrentUser, limit: int = Query(30, ge=1, le=100)) -> list[FamilyMessage]:
    """Family thread (oldest first). Live updates come over the WebSocket; this is for badges and first load."""
    load_elder(user, elder_id)
    return family.recent(elder_id, limit)
