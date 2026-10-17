"""POST /stt?lang=hi-IN — speech to text for browsers without working speech recognition (Brave, Firefox).

The app records the elder (stops after a pause), converts it to 16 kHz WAV and sends it here; Gemini writes down
exactly what was said. Chrome, Edge and Safari keep using their own built-in recognition, which is faster.
"""
from __future__ import annotations

import array
import asyncio
import io
import logging
import wave

from fastapi import APIRouter, HTTPException, Request, status
from google.genai import types
from pydantic import BaseModel

from app import ratelimit, usage
from app.agent.llm import genai_client
from app.auth import CurrentUser
from app.config import get_settings
from app.routers.elders import Language

router = APIRouter(tags=["voice"])
log = logging.getLogger(__name__)

MAX_BYTES = 2 * 1024 * 1024  # 30 s of 16 kHz mono 16-bit WAV is ~0.96 MB
_MIMES = {"audio/wav", "audio/x-wav", "audio/ogg", "audio/mpeg", "audio/mp4", "audio/aac", "audio/flac"}
_SCRIPT = {
    "en-US": "English", "hi-IN": "Hindi in Devanagari script (English words as they are said)",
    "hi-Latn": "Hinglish: Hindi written in Roman letters, English words as they are",
    "th-TH": "Thai", "ja-JP": "Japanese", "ko-KR": "Korean",
}
_PROMPT = (
    "Write down exactly what the speaker says, word for word, as {script}. They are probably speaking that language, "
    "but may mix in others. Only the words — no notes, no translation, no quotes. If nothing is said, or it is only "
    "noise, return an empty string."
)


class Transcript(BaseModel):
    text: str


def silent(audio: bytes) -> bool:
    """A WAV with no sound in it. Given silence, the model makes words up ("हाफ"), so don't ask it."""
    try:
        with wave.open(io.BytesIO(audio)) as w:
            if w.getsampwidth() != 2:
                return False
            pcm = array.array("h", w.readframes(w.getnframes()))
    except (wave.Error, EOFError):
        return False
    return not pcm or max(abs(x) for x in pcm) < 600  # ~-35 dBFS peak: nothing anyone said


def transcribe(audio: bytes, mime: str, lang: str) -> str:
    if mime in ("audio/wav", "audio/x-wav") and silent(audio):
        return ""
    model = get_settings().gemini_model
    r = genai_client().models.generate_content(
        model=model, contents=[types.Part.from_bytes(data=audio, mime_type=mime), "Transcribe this."],
        config=types.GenerateContentConfig(system_instruction=_PROMPT.format(script=_SCRIPT.get(lang, "English")),
                                           temperature=0),
    )
    usage.gemini(model, r, "stt")
    return (r.text or "").strip().strip('"“”').strip()


@router.post("/stt")
async def stt(request: Request, user: CurrentUser, lang: Language = "en-US") -> Transcript:
    mime = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if mime not in _MIMES:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Send the recording as WAV")
    if int(request.headers.get("content-length") or 0) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Recording too long")
    audio = await request.body()
    if not audio or len(audio) > MAX_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE if audio else status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "Recording too long" if audio else "Empty recording")
    ratelimit.tts_calls.check(user.uid)  # same budget as read-aloud: one per spoken turn
    try:
        return Transcript(text=await asyncio.to_thread(transcribe, audio, mime, lang))
    except Exception:  # noqa: BLE001 — the app says "try again" and the buttons still work
        log.exception("speech-to-text failed (%s, %d bytes)", lang, len(audio))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't hear that — try again") from None
