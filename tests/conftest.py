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
