from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.llm import AgentReply, get_chat_model
from app.auth import AuthedUser, get_authed_user
from app.checkin import CheckIn
from app.main import app
from app.routers import insights_api

CARETAKER = AuthedUser(uid="c1", email=None, provider="google.com", role="caretaker")
TODAY = dt.date(2026, 10, 20)


def ci(days_ago: int, mood: str = "good") -> CheckIn:
    d = TODAY - dt.timedelta(days=days_ago)
    return CheckIn(id=f"e_{d}", elderId="e1", date=d, status="done", mood=mood, sleepQuality="well",  # type: ignore[arg-type]
                   meals="rice", medicationTaken=True, startedAt=dt.datetime(2026, 10, 1, tzinfo=dt.UTC))


DRIFTING = [ci(i) for i in range(2, 14)] + [ci(1, "low"), ci(0, "low")]


class Doc:
    def __init__(self, store: dict[str, Any], key: str) -> None:
        self.store, self.key = store, key
    def get(self) -> Any:
        data = self.store.get(self.key)
        return type("S", (), {"to_dict": lambda _s: data})()
    def set(self, data: dict[str, Any]) -> None:
        self.store[self.key] = data


class Model:
    calls = 0
    async def complete(self, system: str, history: Any, schema: Any) -> Any:
        Model.calls += 1
        return AgentReply(reply="Rose has felt a little low the last two days. A call would cheer her up.", quickReplies=[])


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"docs": {}, "logs": [], "elder": {"name": "Rose Devi", "timezone": "Asia/Kolkata",
                                                               "sharingTier": "daily_summary", "caretakerIds": ["c1"]}}
    monkeypatch.setattr(insights_api, "load_elder", lambda user, eid, **_: state["elder"])
    monkeypatch.setattr(insights_api, "local_today", lambda tz: TODAY)
    monkeypatch.setattr(insights_api.checkin_store, "recent_checkins", lambda eid, days: [c for c in DRIFTING if c.date in days])
    monkeypatch.setattr(insights_api.checkin_store, "add_log", lambda eid, e: state["logs"].append(e))
    monkeypatch.setattr(insights_api, "_summary_doc", lambda eid, day: Doc(state["docs"], f"sum-{day}"))
    monkeypatch.setattr(insights_api, "_flags_doc", lambda eid: Doc(state["docs"], "flags"))
    Model.calls = 0
    app.dependency_overrides[get_authed_user] = lambda: CARETAKER
    app.dependency_overrides[get_chat_model] = lambda: Model()
    yield state
    app.dependency_overrides.clear()


def test_insights_flag_drift_and_summary_is_cached(env: dict[str, Any]) -> None:
    c = TestClient(app)
    body = c.get("/elders/e1/insights").json()
    assert body["status"] == "watch" and body["drifts"][0]["signal"] == "mood"
    assert body["summary"].startswith("Rose has felt")
    c.get("/elders/e1/insights")
    assert Model.calls == 1  # same numbers → cached note, no second model call


def test_alerts_only_tier_sees_status_not_the_details(env: dict[str, Any]) -> None:
    env["elder"]["sharingTier"] = "alerts_only"
    body = TestClient(app).get("/elders/e1/insights").json()
    assert body["status"] == "watch" and body["summary"] is None
    assert body["week"]["moodAvg"] is None and "lower" not in body["drifts"][0]["detail"]
    assert Model.calls == 0


def test_drift_is_logged_once_per_signal_per_day(env: dict[str, Any]) -> None:
    first = insights_api.new_drift_flags("e1", env["elder"])
    again = insights_api.new_drift_flags("e1", env["elder"])
    assert len(first) == 1 and "mood lower than usual for 2 days" in first[0] and again == []
    assert env["logs"][0].kind == "warn"
