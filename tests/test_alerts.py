from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import alerts
from app.agent import live
from app.auth import AuthedUser, get_authed_user
from app.contacts import Contact
from app.main import app
from app.routers import alerts_api

ELDER_USER = AuthedUser(uid="e1", email=None, provider="password", role="elder")
CARETAKER = AuthedUser(uid="c1", email=None, provider="google.com", role="caretaker")
ELDER = {"name": "Rose Devi", "phone": "+91 90000 11111", "timezone": "Asia/Kolkata", "caretakerIds": ["c1"],
         "emergencyContacts": [{"name": "Lakshmi (neighbour)", "phone": "+91 91111 22222", "relationship": "neighbour"}],
         "address": {"line1": "12 MG Road", "city": "Pune"}}


def test_who_to_text_puts_emergency_contacts_first_and_dedupes() -> None:
    people = [Contact(name="Ravi", phone="+91 98765 43210", kind="family", trusted=True),
              Contact(name="Lakshmi again", phone="+91-91111-22222", kind="neighbour", trusted=True),  # same number
              Contact(name="Stranger volunteer", phone="+91 70000 00000", kind="volunteer", trusted=True),
              Contact(name="Untrusted cousin", phone="+91 80000 00000", kind="family", trusted=False)]
    got = alerts.who_to_text(ELDER, people)
    assert got == [("Lakshmi (neighbour)", "+91 91111 22222"), ("Ravi", "+91 98765 43210")]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"alerts": {}, "logs": [], "texts": [], "pushed": [], "user": ELDER_USER}
    monkeypatch.setattr(alerts_api, "load_elder", lambda user, eid, **_: ELDER)
    monkeypatch.setattr(alerts, "save", lambda a: state["alerts"].__setitem__(a.id, a.model_copy(deep=True)))
    monkeypatch.setattr(alerts, "get", lambda _e, aid: state["alerts"].get(aid))
    monkeypatch.setattr(alerts, "open_alerts", lambda _e: [a for a in state["alerts"].values() if a.status != "resolved"])
    monkeypatch.setattr(alerts.contacts, "list_contacts", lambda _e: [Contact(name="Ravi", phone="+91 98765", kind="family", trusted=True)])
    monkeypatch.setattr(alerts, "send_sms", lambda to, body: state["texts"].append((to, body)) or "log")
    monkeypatch.setattr(alerts.checkin_store, "add_log", lambda _e, entry: state["logs"].append(entry))
    monkeypatch.setattr(alerts_api.family, "caretaker_name", lambda uid: "Samarth")

    async def push(frame: dict[str, Any]) -> None:
        state["pushed"].append(frame)

    live.register("e1", push, frozenset({"family"}))
    app.dependency_overrides[get_authed_user] = lambda: state["user"]
    yield state
    app.dependency_overrides.clear()
    live.unregister("e1", push)


def test_sos_alerts_family_texts_contacts_and_gives_emergency_number(env: dict[str, Any]) -> None:
    r = TestClient(app).post("/elders/e1/sos")
    assert r.status_code == 201
    body = r.json()
    assert body["emergencyNumber"] == "112"
    assert body["firstContact"] == {"name": "Lakshmi (neighbour)", "phone": "+91 91111 22222"}
    assert [t[0] for t in env["texts"]] == ["+91 91111 22222", "+91 98765"]
    assert "pressed the SOS button" in env["texts"][0][1] and "12 MG Road" in env["texts"][0][1]
    assert env["logs"][-1].kind == "alert"
    assert env["pushed"][0]["type"] == "alert" and env["pushed"][0]["alert"]["status"] == "active"


def test_pressing_twice_does_not_create_a_second_alert(env: dict[str, Any]) -> None:
    c = TestClient(app)
    first = c.post("/elders/e1/sos").json()["alert"]["id"]
    assert c.post("/elders/e1/sos").json()["alert"]["id"] == first
    assert len(env["alerts"]) == 1 and len(env["texts"]) == 2  # texted once, not twice


def test_only_the_elder_can_press_sos(env: dict[str, Any]) -> None:
    env["user"] = CARETAKER
    assert TestClient(app).post("/elders/e1/sos").status_code == 403


def test_caretaker_acknowledges_and_elder_resolves(env: dict[str, Any]) -> None:
    c = TestClient(app)
    aid = c.post("/elders/e1/sos").json()["alert"]["id"]
    assert c.post(f"/elders/e1/alerts/{aid}", json={"action": "acknowledge"}).status_code == 403  # elder can't ack
    env["user"] = CARETAKER
    acked = c.post(f"/elders/e1/alerts/{aid}", json={"action": "acknowledge"}).json()
    assert acked["status"] == "acknowledged" and acked["acknowledgedByName"] == "Samarth"
    env["user"] = ELDER_USER
    done = c.post(f"/elders/e1/alerts/{aid}", json={"action": "resolve"}).json()
    assert done["status"] == "resolved" and done["resolvedBy"] == "elder"
    assert [p["alert"]["status"] for p in env["pushed"]] == ["active", "acknowledged", "resolved"]
