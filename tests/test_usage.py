from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import firebase, usage
from app.config import get_settings
from app.main import app


@pytest.fixture(autouse=True)
def _clean() -> None:
    usage._pending.clear()


def test_gemini_tokens_are_counted_per_model_family_with_thinking_as_output() -> None:
    r = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=1000, candidates_token_count=200, thoughts_token_count=50))
    usage.gemini("gemini-3.5-flash-lite", r, "chat")
    assert usage._pending["gemini-flash-lite|in"] == 1000 and usage._pending["gemini-flash-lite|out"] == 250
    assert usage._pending["purpose:chat|calls"] == 1


def test_estimated_cost_uses_the_price_table() -> None:
    c = usage.cost({"gemini-flash-lite|in": 1_000_000, "gemini-flash-lite|out": 1_000_000, "tts-chirp3-hd|chars": 100_000,
                    "places-autocomplete|calls": 1000})
    assert c == {"gemini-flash-lite": 1.75, "tts-chirp3-hd": 3.0, "places-autocomplete": 2.83}


def test_a_failed_flush_keeps_the_counts_for_next_time() -> None:
    usage.add("tts-chirp3-hd", chars=10)
    usage.flush()  # tests block the real database → the write fails
    assert usage._pending["tts-chirp3-hd|chars"] == 10


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(firebase, "verify_id_token", lambda t: {"uid": "c1", "email": t, "firebase": {"sign_in_provider": "google.com"}})
    monkeypatch.setattr(firebase, "get_user_doc", lambda uid: {"role": "caretaker"})
    monkeypatch.setattr(get_settings(), "admin_emails", ["boss@example.com"])
    return TestClient(app)


def test_only_admins_see_costs(client: TestClient) -> None:
    assert client.get("/admin/usage", headers={"Authorization": "Bearer someone@example.com"}).status_code == 403
    assert client.get("/me", headers={"Authorization": "Bearer Boss@Example.com"}).json()["admin"] is True
