"""POST /medicines/check — spelling / generic-name check for typed medicine names (app/medicines.py).
POST /medicines/scan  — prescription / medicine-strip photo → draft medicines for the family to review (app/rx_scan.py)."""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app import medicines, ratelimit, rx_scan
from app.auth import CurrentUser
from app.schedule import Meals

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


_MIMES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
_MAX_BYTES = 6 * 1024 * 1024  # the app shrinks photos to ~1600 px first, so real ones are far smaller


class ScanRequest(BaseModel):
    image: str = Field(min_length=100, max_length=_MAX_BYTES * 4 // 3 + 4, description="base64")
    mimeType: str
    meals: Meals = Meals()  # the elder's meal times: "1-0-1" lands at breakfast and dinner
    existing: list[Annotated[str, Field(max_length=120)]] = Field(default_factory=list, max_length=30)  # flags a double dose


@router.post("/scan")
async def scan(body: ScanRequest, user: CurrentUser) -> rx_scan.ScanResult:
    if body.mimeType not in _MIMES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Send a photo (JPEG, PNG, WebP or HEIC)")
    try:
        image = base64.b64decode(body.image, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That photo didn't come through — try again") from None
    ratelimit.rx_scans.check(user.uid)
    try:
        return await asyncio.to_thread(rx_scan.scan, image, body.mimeType, body.meals, body.existing)
    except Exception:  # noqa: BLE001 — the family can always type the medicines in
        log.warning("prescription scan failed", exc_info=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't read the photo right now — try again or type it in") from None
