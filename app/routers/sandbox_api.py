"""POST /demo/sandbox — no sign-in: creates a 'Try with real AI' elder + caretaker for this visitor (app/sandbox.py)."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app import ratelimit, sandbox
from app.tools.weather import get_weather

router = APIRouter(prefix="/demo", tags=["demo"])
log = logging.getLogger(__name__)
_warming: set[asyncio.Task[object]] = set()  # strong refs so background tasks aren't garbage-collected


def _warm_weather(city: str) -> None:
    """Visitors often ask about the weather first; the first lookup for a city is slow (~6s), later ones are cached.
    Fetch it now in the background so their first question is answered at normal speed."""
    async def go() -> None:
        try:
            await get_weather(city)
        except Exception:  # noqa: BLE001 — the tool retries and reports on its own
            log.warning("weather warm-up failed for %r", city, exc_info=True)
    task = asyncio.create_task(go())
    _warming.add(task)
    task.add_done_callback(_warming.discard)


class SandboxRequest(BaseModel):
    language: sandbox.SandboxLanguage = "en-US"


def client_ip(request: Request) -> str:
    """Cloud Run puts the visitor first in X-Forwarded-For."""
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


@router.post("/sandbox", status_code=status.HTTP_201_CREATED)
async def create_sandbox(body: SandboxRequest, request: Request) -> sandbox.Sandbox:
    ratelimit.sandbox_per_ip.check(client_ip(request))
    if not ratelimit.sandbox_global.allow("all"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The live demo is busy — please try again later")
    try:
        await asyncio.to_thread(sandbox.cleanup_expired)
    except Exception:  # noqa: BLE001 — cleanup is best effort; never block a visitor on it
        log.exception("sandbox cleanup failed")
    sb = await asyncio.to_thread(sandbox.create_sandbox, body.language)
    _warm_weather(sandbox.PERSONAS[body.language].city)
    return sb
