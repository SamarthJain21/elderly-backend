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
    monkeypatch.setattr(ratelimit, "sandbox_per_ip", ratelimit.RateLimiter(5, 3600))
    monkeypatch.setattr(ratelimit, "sandbox_global", ratelimit.RateLimiter(300, 86400))
    monkeypatch.setattr(ratelimit, "tts_calls", ratelimit.RateLimiter(60, 60))
