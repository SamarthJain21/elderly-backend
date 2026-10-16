"""POST /demo/sandbox — no sign-in: creates a 'Try with real AI' elder + caretaker for this visitor (app/sandbox.py)."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app import ratelimit, sandbox

router = APIRouter(prefix="/demo", tags=["demo"])
log = logging.getLogger(__name__)


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
    return await asyncio.to_thread(sandbox.create_sandbox, body.language)
