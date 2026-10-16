from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import firebase, voice
from app.main import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(firebase, "verify_id_token",
                        lambda t: {"uid": "e1", "firebase": {"sign_in_provider": "password"}})
    monkeypatch.setattr(firebase, "get_user_doc", lambda uid: {"role": "elder"})
    return TestClient(app)


H = {"Authorization": "Bearer t"}


def test_reads_text_aloud_as_mp3_in_the_elders_language(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(voice, "synthesize", lambda text, lang: calls.append((text, lang)) or b"ID3mp3")
    r = client.post("/tts", headers=H, json={"text": "สวัสดีค่ะ 😊", "language": "th-TH"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content == b"ID3mp3"
    assert calls == [("สวัสดีค่ะ", "th-TH")]          # emoji aren't read out
    assert "private" in r.headers["cache-control"]


def test_needs_sign_in(client: TestClient) -> None:
    assert client.post("/tts", json={"text": "hi"}).status_code == 401


def test_only_emoji_is_nothing_to_read(client: TestClient) -> None:
    assert client.post("/tts", headers=H, json={"text": "👍🙏"}).status_code == 422


def test_voice_failure_is_a_502_so_the_app_uses_the_device_voice(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any) -> bytes:
        raise RuntimeError("quota")
    monkeypatch.setattr(voice, "synthesize", boom)
    assert client.post("/tts", headers=H, json={"text": "hello"}).status_code == 502


def test_repeated_sentences_come_from_the_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    posts: list[dict[str, Any]] = []

    class R:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict[str, str]:
            return {"audioContent": "SUQz"}  # "ID3"

    class S:
        def post(self, url: str, json: dict[str, Any], timeout: int) -> R:
            posts.append(json)
            return R()
    monkeypatch.setattr(voice, "_session", lambda: S())
    voice._cache.clear()
    assert voice.synthesize("Tap Yes or No", "hi-IN") == b"ID3"
    assert voice.synthesize("Tap Yes or No", "hi-IN") == b"ID3"
    assert len(posts) == 1 and posts[0]["voice"]["name"] == "hi-IN-Chirp3-HD-Sulafat"
    assert posts[0]["audioConfig"]["speakingRate"] == voice.SPEAKING_RATE


def test_hinglish_is_spoken_by_the_hindi_voice_after_conversion_to_devanagari(monkeypatch: pytest.MonkeyPatch) -> None:
    posts: list[dict[str, Any]] = []

    class R:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict[str, str]:
            return {"audioContent": "SUQz"}

    class S:
        def post(self, url: str, json: dict[str, Any], timeout: int) -> R:
            posts.append(json)
            return R()
    monkeypatch.setattr(voice, "_session", lambda: S())
    monkeypatch.setattr(voice, "to_devanagari", lambda t: "नमस्ते जी" if t == "Namaste ji" else pytest.fail(t))
    voice._cache.clear()
    voice.synthesize("Namaste ji", "hi-Latn")
    assert posts[0]["input"]["text"] == "नमस्ते जी" and posts[0]["voice"]["languageCode"] == "hi-IN"
    voice.synthesize("Namaste ji", "hi-IN")  # same text, other language: its own clip, no conversion
    assert posts[1]["input"]["text"] == "Namaste ji"
