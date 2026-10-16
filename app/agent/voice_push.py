"""Read the helper's replies aloud with no extra round trip: the server synthesizes the voice the moment a reply is
ready and pushes it to the elder's app over the chat socket, first sentence first (it's ready sooner)."""
from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any

from app import voice
from app.agent import live

log = logging.getLogger(__name__)
_SENTENCE_END = re.compile(r"(?<=[.!?।。！？])\s+")
_tasks: set[asyncio.Task[None]] = set()  # strong refs: background tasks aren't garbage-collected mid-flight


def split_for_speech(text: str) -> list[str]:
    """First sentence, then the rest (2 clips at most, synthesized in parallel). Thai has no sentence punctuation:
    split a long reply at the first space after a short opening phrase."""
    text = text.strip()
    parts = _SENTENCE_END.split(text, maxsplit=1)
    if len(parts) == 1 and len(text) > 60 and " " in text[25:]:
        cut = text.index(" ", 25)
        parts = [text[:cut], text[cut + 1:]]
    return [p.strip() for p in parts if p.strip()]


async def _push(elder_id: str, language: str, message_id: str, text: str, spoken: str | None) -> None:
    # Hinglish with a Devanagari version from the model: speak that with the Hindi voice (no conversion call).
    say, lang = (spoken, "hi-IN") if language == "hi-Latn" and spoken else (text, language)
    clips = split_for_speech(voice.clean(say))
    if not clips:
        return
    jobs = [asyncio.create_task(asyncio.to_thread(voice.synthesize, c, lang)) for c in clips]
    for seq, job in enumerate(jobs):  # in order: clip 2 may finish first but must play second
        try:
            audio = await job
        except Exception:  # noqa: BLE001 — the app falls back to fetching the voice itself
            log.warning("voice push failed for message %s (clip %d)", message_id, seq, exc_info=True)
            for j in jobs[seq + 1:]:
                j.cancel()
            return
        await live.broadcast(elder_id, {"type": "audio", "messageId": message_id, "seq": seq,
                                        "last": seq == len(jobs) - 1, "mime": "audio/mpeg",
                                        "data": base64.b64encode(audio).decode()})


def speak_reply(elder_id: str, elder: dict[str, Any], message_id: str, text: str, spoken: str | None = None) -> None:
    """Start synthesizing now, in the background; never blocks or fails the turn."""
    if not text.strip():
        return
    task = asyncio.create_task(_push(elder_id, elder.get("language", "en-US"), message_id, text, spoken))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
