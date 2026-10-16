"""POST /tts — read text aloud in a natural voice (app/voice.py). Any signed-in user."""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from app import ratelimit, voice
from app.auth import CurrentUser

router = APIRouter(tags=["voice"])
log = logging.getLogger(__name__)


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=800)
    language: Literal["en-US", "hi-IN", "th-TH", "ja-JP", "ko-KR"] = "en-US"


@router.post("/tts", response_class=Response, responses={200: {"content": {"audio/mpeg": {}}}})
async def tts(body: SpeakRequest, user: CurrentUser) -> Response:
    ratelimit.tts_calls.check(user.uid)
    text = voice.clean(body.text)
    if not text:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Nothing to read aloud")
    try:
        audio = await asyncio.to_thread(voice.synthesize, text, body.language)
    except Exception:  # noqa: BLE001 — the app falls back to the device's own voice
        log.exception("text-to-speech failed (%s, %d chars)", body.language, len(text))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Voice unavailable") from None
    # Private: the text can be personal. The app keeps its own copy for replays.
    return Response(audio, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})
