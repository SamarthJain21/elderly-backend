from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthedUser, get_authed_user
from app.main import app
from app.routers import stt_api

ELDER = AuthedUser(uid="e1", email=None, provider="password", role="elder")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    calls: list[Any] = []
    monkeypatch.setattr(stt_api, "transcribe", lambda audio, mime, lang: calls.append((len(audio), mime, lang)) or "नमस्ते")
    app.dependency_overrides[get_authed_user] = lambda: ELDER
    yield calls
    app.dependency_overrides.clear()


def test_a_wav_recording_comes_back_as_text_in_the_elders_language(client: list[Any]) -> None:
    r = TestClient(app).post("/stt?lang=hi-IN", content=b"RIFF" + b"\0" * 400, headers={"Content-Type": "audio/wav"})
    assert r.json() == {"text": "नमस्ते"} and client == [(404, "audio/wav", "hi-IN")]


def test_webm_empty_and_oversized_recordings_are_refused(client: list[Any]) -> None:
    c = TestClient(app)
    assert c.post("/stt", content=b"x" * 10, headers={"Content-Type": "audio/webm"}).status_code == 415
    assert c.post("/stt", content=b"", headers={"Content-Type": "audio/wav"}).status_code == 422
    assert c.post("/stt", content=b"x" * (stt_api.MAX_BYTES + 1), headers={"Content-Type": "audio/wav"}).status_code == 413
    assert not client


def test_a_failed_transcription_is_a_friendly_retry(monkeypatch: pytest.MonkeyPatch, client: list[Any]) -> None:
    def boom(*_a: Any) -> str:
        raise RuntimeError("vertex down")
    monkeypatch.setattr(stt_api, "transcribe", boom)
    r = TestClient(app).post("/stt", content=b"RIFF" + b"\0" * 40, headers={"Content-Type": "audio/wav"})
    assert r.status_code == 502 and "try again" in r.json()["detail"]


def test_silence_is_empty_without_asking_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import wave

    def wav(sample: int) -> bytes:
        b = io.BytesIO()
        with wave.open(b, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)  # noqa: E702
            w.writeframes(sample.to_bytes(2, "little", signed=True) * 16000)
        return b.getvalue()

    monkeypatch.setattr(stt_api, "genai_client", lambda: (_ for _ in ()).throw(AssertionError("model called")))
    assert stt_api.transcribe(wav(3), "audio/wav", "hi-IN") == ""
    assert stt_api.silent(wav(3)) and not stt_api.silent(wav(8000))
