from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import pytest

from app import voice
from app.agent import live
from app.agent import voice_push as vp

# The autouse fixture stubs speak_reply; these tests call _push directly.


def test_split_first_sentence_then_the_rest() -> None:
    assert vp.split_for_speech("Hello Rose. Your order is on its way. Anything else?") == \
        ["Hello Rose.", "Your order is on its way. Anything else?"]
    assert vp.split_for_speech("नमस्ते जी। दवाई का समय हो गया है।") == ["नमस्ते जी।", "दवाई का समय हो गया है।"]
    assert vp.split_for_speech("Okay!") == ["Okay!"]


def test_long_thai_without_punctuation_is_split_at_a_space() -> None:
    th = "คุณสมศรีคะ สั่งข้าวมันไก่จาก LINE MAN ราคา ฿72 ได้ค่ะ ตกลงสั่งเลยไหมคะ"
    parts = vp.split_for_speech(th)
    assert len(parts) == 2 and " ".join(parts) == th and len(parts[0]) >= 25


@pytest.fixture
def frames(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    got: list[dict[str, Any]] = []

    async def push(frame: dict[str, Any]) -> None:
        got.append(frame)
    live.register("e1", push, frozenset({"agent"}))
    yield got
    live.unregister("e1", push)


@pytest.mark.anyio
async def test_clips_arrive_in_order_even_if_the_second_is_ready_first(monkeypatch: pytest.MonkeyPatch, frames: list[dict[str, Any]]) -> None:
    def synth(text: str, lang: str) -> bytes:
        time.sleep(0.15 if text.startswith("Hello") else 0.01)  # first clip is slower
        return f"{lang}:{text}".encode()
    monkeypatch.setattr(voice, "synthesize", synth)
    await vp._push("e1", "en-US", "m1", "Hello Rose. Your tea is coming.", None)
    assert [(f["seq"], f["last"], base64.b64decode(f["data"]).decode()) for f in frames] == \
        [(0, False, "en-US:Hello Rose."), (1, True, "en-US:Your tea is coming.")]
    assert all(f["type"] == "audio" and f["messageId"] == "m1" for f in frames)


@pytest.mark.anyio
async def test_hinglish_uses_the_models_devanagari_with_the_hindi_voice(monkeypatch: pytest.MonkeyPatch, frames: list[dict[str, Any]]) -> None:
    langs: list[str] = []
    monkeypatch.setattr(voice, "synthesize", lambda t, lang: langs.append(lang) or b"x")
    await vp._push("e1", "hi-Latn", "m2", "Namaste ji! Dawai le li?", "नमस्ते जी! दवाई ले ली?")
    assert langs == ["hi-IN", "hi-IN"]  # no Hinglish→Devanagari conversion call needed


@pytest.mark.anyio
async def test_a_failed_clip_stops_quietly_so_the_app_falls_back(monkeypatch: pytest.MonkeyPatch, frames: list[dict[str, Any]]) -> None:
    def boom(t: str, lang: str) -> bytes:
        raise RuntimeError("quota")
    monkeypatch.setattr(voice, "synthesize", boom)
    await vp._push("e1", "en-US", "m3", "Hello. Bye.", None)
    assert frames == []
