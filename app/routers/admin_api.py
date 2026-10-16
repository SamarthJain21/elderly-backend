"""GET /admin/usage — API usage and estimated cost per day (app/usage.py). Admins only (ADMIN_EMAILS)."""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from app import firebase, usage
from app.auth import CurrentUser
from app.config import get_settings

router = APIRouter(prefix="/admin", tags=["admin"])


def is_admin(email: str | None) -> bool:
    return bool(email) and email.lower() in {e.lower() for e in get_settings().admin_emails}


@router.get("/usage")
async def usage_report(user: CurrentUser, days: Annotated[int, Query(ge=1, le=90)] = 14) -> dict[str, Any]:
    if not is_admin(user.email):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    await asyncio.to_thread(usage.flush)  # include the last minute
    today = dt.datetime.now(dt.UTC).date()
    ids = [(today - dt.timedelta(days=i)).isoformat() for i in range(days)]
    db = firebase.get_db()
    snaps = await asyncio.to_thread(lambda: list(db.get_all([db.collection("usage").document(d) for d in ids])))
    by_day = {s.id: (s.to_dict() or {}) for s in snaps if s.exists}
    rows = []
    for d in ids:
        counts = {k: int(v) for k, v in by_day.get(d, {}).items()}
        c = usage.cost(counts)
        rows.append({"date": d, "counts": counts, "costUsd": c, "totalUsd": round(sum(c.values()), 4)})
    return {"days": rows, "totalUsd": round(sum(r["totalUsd"] for r in rows), 4), "prices": usage.PRICES,
            "note": "Estimates from counted usage and the price table — Google Cloud Billing is the source of truth."}
