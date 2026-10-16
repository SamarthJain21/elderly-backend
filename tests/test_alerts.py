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
    # SMS isn't configured in tests: the family's log says so plainly, once, not per person.
    assert env["logs"][-1].message.endswith("Lakshmi (neighbour) and Ravi are not texted yet — SMS isn't switched on.")


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


def test_elder_profile_includes_caretaker_names_but_no_contact_details(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import firebase
    from app.routers import elders

    class Snap:
        def __init__(self, data: dict[str, Any] | None) -> None:
            self._d, self.exists = data, data is not None
        def to_dict(self) -> dict[str, Any] | None:
            return self._d

    class Doc:
        def __init__(self, data: dict[str, Any] | None) -> None:
            self._d = data
        def get(self) -> Snap:
            return Snap(self._d)

    caretakers = {"c1": {"name": "Samarth", "email": "s@example.com", "photoUrl": "https://lh3.googleusercontent.com/a/s"},
                  "c2": {"name": "Priya", "email": "p@example.com"}}

    class Col:
        def __init__(self, name: str) -> None:
            self.name = name
        def document(self, did: str) -> Any:
            return ("caretaker", did) if self.name == "caretakers" else Doc({})

    class DB:
        def collection(self, name: str) -> Col:
            return Col(name)
        def get_all(self, refs: list[Any]) -> list[Snap]:
            return [Snap(caretakers.get(r[1])) for r in refs]

    elder = {**ELDER, "name": "Rose", "language": "en-US", "caretakerIds": ["c1", "c2", "gone"]}
    monkeypatch.setattr(elders, "load_elder", lambda user, eid, **_: elder)
    monkeypatch.setattr(firebase, "get_db", lambda: DB())
    app.dependency_overrides[get_authed_user] = lambda: ELDER_USER
    try:
        body = TestClient(app).get("/elders/e1").json()
    finally:
        app.dependency_overrides.clear()
    assert body["caretakerNames"] == ["Samarth", "Priya"]
    assert body["family"] == [{"name": "Samarth", "photo": "https://lh3.googleusercontent.com/a/s"}, {"name": "Priya", "photo": None}]
    assert "s@example.com" not in str(body)
    assert "s@example.com" not in str(body)


def test_names_reads_naturally() -> None:
    assert [alerts.names(x) for x in (["Emma"], ["Emma", "Tom"], ["Emma", "Tom", "David"])] == \
        ["Emma", "Emma and Tom", "Emma, Tom and David"]


def test_finished_walkthrough_is_remembered_on_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from app.routers import elders
    updates: list[dict[str, Any]] = []
    monkeypatch.setattr(elders, "load_elder", lambda *a, **k: {"name": "Rose"})
    monkeypatch.setattr(elders.firebase, "get_db", lambda: SimpleNamespace(collection=lambda _: SimpleNamespace(
        document=lambda _: SimpleNamespace(update=lambda d: updates.append(d)))))
    app.dependency_overrides[get_authed_user] = lambda: ELDER_USER
    try:
        assert TestClient(app).post("/elders/e1/app-ready").status_code == 204
    finally:
        app.dependency_overrides.clear()
    assert "appReadyAt" in updates[0]
