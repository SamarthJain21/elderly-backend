import pytest

from app import firebase


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_real_firestore(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never read or write the real database (they also run in CI with cloud credentials).
    Anything that reaches Firestore without being faked fails loudly here."""
    def blocked() -> None:
        raise RuntimeError("test tried to use the real Firestore — fake the store function instead")
    monkeypatch.setattr(firebase, "get_db", blocked)


@pytest.fixture(autouse=True)
def _fresh_rate_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Limiters are process-global; give every test its own so counts never leak between tests."""
    from app import ratelimit
    monkeypatch.setattr(ratelimit, "chat_frames", ratelimit.RateLimiter(20, 60))
    monkeypatch.setattr(ratelimit, "account_writes", ratelimit.RateLimiter(10, 3600))
    monkeypatch.setattr(ratelimit, "sandbox_per_ip", ratelimit.RateLimiter(20, 3600))
    monkeypatch.setattr(ratelimit, "sandbox_global", ratelimit.RateLimiter(300, 86400))
    monkeypatch.setattr(ratelimit, "tts_calls", ratelimit.RateLimiter(60, 60))
    monkeypatch.setattr(ratelimit, "ask_calls", ratelimit.RateLimiter(30, 3600))
    monkeypatch.setattr(ratelimit, "places_calls", ratelimit.RateLimiter(60, 60))
    monkeypatch.setattr(ratelimit, "rx_scans", ratelimit.RateLimiter(20, 3600))


@pytest.fixture(autouse=True)
def _no_voice_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chat tests count frames and must not call Text-to-Speech; tests/test_voice_push.py covers the push itself."""
    from app.agent import voice_push
    monkeypatch.setattr(voice_push, "speak_reply", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _no_push(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record family notifications instead of sending them (tests/test_push.py covers sending)."""
    from app import push
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(push, "notify_family", lambda elder, title, body, path="/caretaker": sent.append((title, body)))
    return sent


@pytest.fixture(autouse=True)
def _no_translate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Family-chat translation calls Gemini; tests get a marker instead (tests/test_translate.py covers it)."""
    from app.agent import translate
    monkeypatch.setattr(translate, "translate", lambda text, target, source=None, writer=None, context=None: f"[{target}] {text}" if translate.needs_translation(text, target, source) else None)
