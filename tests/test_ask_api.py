from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthedUser, get_authed_user
from app.checkin import CheckIn
from app.checkin_store import LogEntry
from app.main import app
from app.routers import ask_api

CARETAKER = AuthedUser(uid="c1", email=None, provider="google.com", role="caretaker")
TODAY = dt.date(2026, 10, 17)


def ci(days_ago: int, **kw: Any) -> CheckIn:
    d = TODAY - dt.timedelta(days=days_ago)
    return CheckIn(id=f"e1_{d}", elderId="e1", date=d, status="done", startedAt=dt.datetime(2026, 10, 1, tzinfo=dt.UTC), **kw)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"elder": {"name": "Kamla Devi", "timezone": "Asia/Kolkata",
                                       "medicationSchedule": [{"name": "Telma 40", "dose": ""}]}, "calls": []}

    class Models:
        def generate_content(self, **kw: Any) -> Any:
            state["calls"].append(kw)
            return type("R", (), {"text": state.get("answer", "She slept badly on Thursday and Friday."), "usage_metadata": None})()

    monkeypatch.setattr(ask_api, "load_elder", lambda user, eid, **_: state["elder"])
    monkeypatch.setattr(ask_api, "local_today", lambda tz: TODAY)
    monkeypatch.setattr(ask_api.checkin_store, "recent_checkins",
                        lambda eid, days: [ci(1, mood="low", sleepQuality="poorly"), ci(2, mood="good", anomalies=["knee pain"])])
    monkeypatch.setattr(ask_api.checkin_store, "recent_logs", lambda eid, n: [LogEntry(message="Ordered dal khichdi", kind="ok")])
    monkeypatch.setattr(ask_api, "genai_client", lambda: type("C", (), {"models": Models()})())
    app.dependency_overrides[get_authed_user] = lambda: CARETAKER
    yield state
    app.dependency_overrides.clear()


def test_answers_from_checkins_and_activity_only(env: dict[str, Any]) -> None:
    r = TestClient(app).post("/elders/e1/ask", json={"question": "Has Mum been sleeping well?"})
    assert r.status_code == 200
    assert r.json() == {"answer": "She slept badly on Thursday and Friday.", "days": 2}
    sent = env["calls"][0]
    assert sent["contents"] == "Has Mum been sleeping well?"
    system = sent["config"].system_instruction
    assert "Kamla" in system and "Saturday" in system                     # knows who and what "today" is
    assert "knee pain" in system and "Ordered dal khichdi" in system and "Telma 40" in system
    assert "Never diagnose" in system


def test_alerts_only_sharing_has_nothing_to_ask_about(env: dict[str, Any]) -> None:
    env["elder"]["sharingTier"] = "alerts_only"
    r = TestClient(app).post("/elders/e1/ask", json={"question": "How is she?"})
    assert r.status_code == 403 and not env["calls"]


def test_empty_answer_is_a_retryable_error(env: dict[str, Any]) -> None:
    env["answer"] = "  "
    assert TestClient(app).post("/elders/e1/ask", json={"question": "How is she?"}).status_code == 502


def test_question_length_is_checked(env: dict[str, Any]) -> None:
    assert TestClient(app).post("/elders/e1/ask", json={"question": "x" * 201}).status_code == 422
