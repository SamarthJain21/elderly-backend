"""GET /places/autocomplete, GET /places/{placeId} — address search for the family's forms (app/tools/places.py)."""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from app import ratelimit
from app.auth import CurrentUser
from app.tools import places

router = APIRouter(prefix="/places", tags=["places"])
log = logging.getLogger(__name__)


@router.get("/autocomplete")
async def autocomplete(user: CurrentUser, q: Annotated[str, Query(min_length=3, max_length=120)],
                       country: Annotated[str | None, Query(pattern="^[A-Za-z]{2}$")] = None,
                       session: Annotated[str | None, Query(max_length=64)] = None) -> list[dict[str, str]]:
    ratelimit.places_calls.check(user.uid)
    try:
        return await asyncio.to_thread(places.autocomplete, q, country, session)
    except Exception:  # noqa: BLE001 — the form still works by typing
        log.warning("places autocomplete failed", exc_info=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Address search is unavailable — type the address instead") from None


@router.get("/{place_id}")
async def place(place_id: str, user: CurrentUser, session: Annotated[str | None, Query(max_length=64)] = None) -> dict[str, Any]:
    ratelimit.places_calls.check(user.uid)
    try:
        return await asyncio.to_thread(places.details, place_id, session)
    except Exception:  # noqa: BLE001
        log.warning("places details failed", exc_info=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't load that address — type it instead") from None
