"""POST /medicines/check — spelling / generic-name check for typed medicine names (app/medicines.py)."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app import medicines, ratelimit
from app.auth import CurrentUser

router = APIRouter(prefix="/medicines", tags=["medicines"])
log = logging.getLogger(__name__)


class CheckRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    country: str = Field(default="India", max_length=40)


@router.post("/check")
async def check(body: CheckRequest, user: CurrentUser) -> medicines.MedicineCheck:
    ratelimit.places_calls.check(user.uid)  # same light budget as address search
    try:
        return await asyncio.to_thread(medicines.check, body.name.strip(), body.country)
    except Exception:  # noqa: BLE001 — it's only a helper; the form works without it
        log.warning("medicine check failed", exc_info=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't check that name right now") from None
