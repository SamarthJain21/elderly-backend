from __future__ import annotations

import contextlib

import datetime as dt
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.agent.llm import AgentReply, CheckInTurn, Turn, get_chat_model
from app.agent.messages import Message
from app.agent.prompt import build_system_prompt
from app.auth import AuthedUser
from app.main import app
from app.routers import chat
from app.autonomy import default_rules
from app.checkin import CheckIn, Extracted
from app.checkin_store import LogEntry
from app.schedule import Reminder
from app.tools import executor
from app.tools.geo import Place

ELDER = AuthedUser(uid="e1", email=None, provider="password", role="elder")
ELDER_DOC = {
    "name": "Rose Devi", "language": "hi-IN", "timezone": "Asia/Kolkata", "caretakerIds": ["c1"],
    "medicationSchedule": [{"name": "Amlodipine", "dose": "5mg", "times": ["08:00"], "withFood": False}],
    "address": {"line1": "12 MG Road", "line2": "Flat 3", "landmark": "", "city": "Pune", "postcode": "411001",
                "lat": 18.52, "lng": 73.85, "precision": "street"},
}


class FakeModel:
    def __init__(self, fail: bool = False, extracted: list[dict[str, Any]] | None = None,
                 tool_calls: list[tuple[str, dict[str, Any]] | None] | None = None) -> None:
        self.calls: list[tuple[str, list[Turn]]] = []
        self.fail = fail
        self.extracted = list(extracted or [])  # one per check-in turn
        self.tool_calls = list(tool_calls or [])  # one per chat turn; None = just reply
        self.tool_results: list[dict[str, Any]] = []
        self.offered: list[list[str]] = []

    async def run(self, system: str, history: list[Turn], tools: list[dict[str, Any]], execute: Any) -> AgentReply:
        self.calls.append((system, history))
        self.offered.append([t["name"] for t in tools])
        if self.fail:
            raise RuntimeError("model down")
        call = self.tool_calls.pop(0) if self.tool_calls else None
        if call is None:
            return AgentReply(reply="नमस्ते!", quickReplies=["अच्छा", "ठीक हूँ", "धन्यवाद"])
        result = await execute(*call)
        self.tool_results.append(result)
        return AgentReply(reply=f"status={result['status']}", quickReplies=["Yes", "No", "Later"])

    async def complete(self, system: str, history: list[Turn], schema: type[Any]) -> Any:
        self.calls.append((system, history))
        if self.fail:
            raise RuntimeError("model down")
        if schema is CheckInTurn:
            return CheckInTurn(reply="Thanks!", quickReplies=["a", "b", "c"], extracted=Extracted(**self.extracted.pop(0)))
        return schema(reply="नमस्ते!", quickReplies=["अच्छा", "ठीक हूँ", "धन्यवाद"])


class Store(dict[str, list[Message]]):
    alerts: list[Any]
    reminders: list[tuple[str, str, Any]]
    people: list[Any]
    requests: dict[str, Any]
    texts: list[tuple[str, str]]
    family: list[Any]
    actions: dict[str, Any]
    rules: Any
    checkins: dict[str, CheckIn]
    logs: list[LogEntry]
    transcripts: dict[str, str]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    db = Store({"e1": []})
    db.checkins, db.logs, db.transcripts = {}, [], {}
    # Check-in at 00:00 every day, so one is always startable (due or missed) whenever tests run.
    monkeypatch.setattr(chat.checkin_store, "schedule_of", lambda _eid: ("00:00", ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]))
    monkeypatch.setattr(chat.checkin_store, "get_checkin", lambda cid: db.checkins.get(cid))
    monkeypatch.setattr(chat.checkin_store, "save_checkin", lambda c: db.checkins.__setitem__(c.id, c))
    monkeypatch.setattr(chat.checkin_store, "add_log", lambda _eid, e: db.logs.append(e))
    monkeypatch.setattr(chat.checkin_store, "save_transcript", lambda c, t: db.transcripts.__setitem__(c.id, t))
    # Agent actions + rules, in memory.
    db.actions = {}
    monkeypatch.setattr(chat, "load_rules", lambda _eid, _elder: db.rules)
    db.rules = default_rules("INR")
    monkeypatch.setattr(chat.actions, "save", lambda a: db.actions.__setitem__(a.id, a.model_copy(deep=True)))
    monkeypatch.setattr(chat.actions, "get", lambda _eid, aid: db.actions[aid].model_copy(deep=True) if aid in db.actions else None)
    monkeypatch.setattr(chat.actions, "with_status", lambda _eid, st: [a for a in db.actions.values() if a.status == st])
    monkeypatch.setattr(chat.messages, "set_action_card", lambda _eid, mid, card: None)
    db.family = []
    monkeypatch.setattr(chat.family, "save", lambda _eid, m: db.family.append(m))
    monkeypatch.setattr(chat.family, "recent", lambda _eid, n: db.family[-n:])
    monkeypatch.setattr(chat.family, "caretaker_name", lambda uid: "Emma")
    # Return copies, like Firestore does — mutating a shared object would hide double-marking bugs.
    monkeypatch.setattr(chat.family, "get", lambda _eid, mid: next((m.model_copy(deep=True) for m in db.family if m.id == mid), None))

    def mark_used(_eid: str, mid: str, idx: int) -> bool:
        for m in db.family:
            if m.id == mid:
                if idx in m.usedChips:
                    return False
                m.usedChips.append(idx)
                return True
        return False
    monkeypatch.setattr(chat.family, "mark_used", mark_used)
    db.reminders = []
    monkeypatch.setattr(chat, "save_reminder", lambda eid, title, time, day: db.reminders.append((title, time, day)))

    async def fake_geocode(q: str, tz: Any = None, near: Any = None) -> Place | None:
        return None if "nowhere" in q else Place(lat=18.53, lng=73.88, name=f"{q} (OSM)")
    monkeypatch.setattr(executor, "geocode", fake_geocode)
    db.people, db.requests, db.texts = [], {}, []
    monkeypatch.setattr(chat.contacts, "list_contacts", lambda _eid: list(db.people))
    monkeypatch.setattr(chat.contacts, "save_request", lambda _eid, r: db.requests.__setitem__(r.id, r))
    monkeypatch.setattr(executor, "send_sms", lambda to, body: db.texts.append((to, body)) or "log")
    # Emergencies go through app.alerts — keep them in memory too (never the real Firestore).
    db.alerts = []
    monkeypatch.setattr(chat.alerts_mod, "save", lambda a: db.alerts.append(a))
    monkeypatch.setattr(chat.alerts_mod, "send_sms", lambda to, body: db.texts.append((to, body)) or "log")

    def fake_auth(token: str) -> AuthedUser:
        if token != "elder-token":
            raise HTTPException(401)
        return ELDER

    def fake_load(user: AuthedUser, elder_id: str, **_: Any) -> dict[str, Any]:
        if elder_id != user.uid:
            raise HTTPException(404)
        return ELDER_DOC

    monkeypatch.setattr(chat, "authenticate", fake_auth)
    monkeypatch.setattr(chat, "load_elder", fake_load)
    monkeypatch.setattr(chat, "all_reminders", lambda _eid: [])
    monkeypatch.setattr(chat.messages, "save", lambda eid, m: db[eid].append(m))
    monkeypatch.setattr(chat.messages, "recent", lambda eid, n: db[eid][-n:])
    return db


@pytest.fixture
def model() -> FakeModel:
    m = FakeModel()
    app.dependency_overrides[get_chat_model] = lambda: m
    yield m
    app.dependency_overrides.clear()


def test_rejects_connection_that_does_not_auth_first(store: Any, model: FakeModel) -> None:
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        ws.send_json({"type": "message", "text": "hi"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_rejects_bad_token(store: Any, model: FakeModel) -> None:
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        ws.send_json({"type": "auth", "token": "nope"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4401


def test_rejects_other_elders_chat(store: Any, model: FakeModel) -> None:
    with TestClient(app).websocket_connect("/ws/someone-else") as ws:
        ws.send_json({"type": "auth", "token": "elder-token"})
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
    assert exc.value.code == 4403


def test_message_round_trip_saves_both_sides(store: dict[str, list[Message]], model: FakeModel) -> None:
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        ws.send_json({"type": "auth", "token": "elder-token"})
        assert ws.receive_json() == {"type": "history", "messages": []}
        assert ws.receive_json() == {"type": "family_history", "messages": []}
        assert ws.receive_json()["type"] == "checkin"
        ws.send_json({"type": "message", "text": "  hello  "})
        echo = ws.receive_json()
        assert echo["message"]["sender"] == "elder" and echo["message"]["text"] == "hello"
        assert ws.receive_json() == {"type": "typing"}
        reply = ws.receive_json()["message"]
        assert reply["sender"] == "agent"
        assert reply["quickReplies"] == ["अच्छा", "ठीक हूँ", "धन्यवाद"]
    assert [m.sender for m in store["e1"]] == ["elder", "agent"]
    _, turns = model.calls[0]
    assert turns == [Turn(role="elder", text="hello")]


def test_model_failure_reports_error_and_keeps_socket_open(store: dict[str, list[Message]]) -> None:
    app.dependency_overrides[get_chat_model] = lambda: FakeModel(fail=True)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            ws.send_json({"type": "auth", "token": "elder-token"})
            ws.receive_json(), ws.receive_json(), ws.receive_json()
            ws.send_json({"type": "message", "text": "hi"})
            ws.receive_json(), ws.receive_json()  # echo, typing
            assert ws.receive_json()["type"] == "error"
            ws.send_json({"type": "message", "text": "still there?"})
            assert ws.receive_json()["type"] == "message"
    finally:
        app.dependency_overrides.clear()
    assert [m.sender for m in store["e1"]] == ["elder", "elder"]  # no fake agent message saved


def test_rejects_empty_and_oversized_messages(store: Any, model: FakeModel) -> None:
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        ws.send_json({"type": "auth", "token": "elder-token"})
        ws.receive_json(), ws.receive_json(), ws.receive_json()
        ws.send_json({"type": "message", "text": ""})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"type": "message", "text": "x" * 1001})
        assert ws.receive_json()["type"] == "error"
    assert model.calls == []


def test_history_sent_to_model_starts_with_elder_turn(store: dict[str, list[Message]], model: FakeModel) -> None:
    store["e1"] += [Message(sender="agent", text="Good morning!"), Message(sender="elder", text="hi")]
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        ws.send_json({"type": "auth", "token": "elder-token"})
        assert len(ws.receive_json()["messages"]) == 2
        ws.receive_json(), ws.receive_json()
        ws.send_json({"type": "message", "text": "how are you"})
        for _ in range(3):
            ws.receive_json()
    _, turns = model.calls[0]
    assert turns[0].role == "elder"


def test_prompt_uses_language_local_time_meds_and_reminders() -> None:
    elder = {
        **ELDER_DOC,
        "sleep": {"bedtime": "22:00", "wakeTime": "06:30"},
        "medicationSchedule": [{"name": "Amlodipine", "dose": "5mg", "times": ["08:00", "20:00"], "withFood": True}],
    }
    today = [Reminder(id="r", elderId="e1", title="Doctor visit", kind="appointment", time="10:00", date=dt.date(2026, 10, 15))]
    p = build_system_prompt(elder, today, dt.datetime(2026, 10, 14, 20, 0, tzinfo=dt.UTC))
    assert "Hindi" in p
    assert "Thursday 15 October 2026, 01:30" in p  # 20:00 UTC = 01:30 IST next day
    assert "Amlodipine 5mg at 08:00, 20:00 with food" in p
    assert "10:00 Doctor visit (later today)" in p  # 01:30 now, so 10:00 is still ahead
    assert "Call them Rose" in p


class _FakeModels:
    def __init__(self, outcomes: dict[str, Exception | str]) -> None:
        self.outcomes = outcomes
        self.tried: list[str] = []

    async def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        self.tried.append(model)
        out = self.outcomes[model]
        if isinstance(out, Exception):
            raise out

        class R:
            parsed = None
            text = out
        return R()


def _client(models: _FakeModels) -> Any:
    class C:
        class aio:
            pass
    C.aio.models = models  # type: ignore[attr-defined]
    return C


@pytest.mark.anyio
async def test_falls_back_to_next_model_when_primary_is_overloaded() -> None:
    from google.genai import errors

    from app.agent.llm import GeminiChatModel
    ok = '{"reply": "Hi", "quickReplies": ["a", "b", "c", "d"]}'
    fake = _FakeModels({"big": errors.ServerError(503, {"error": {"message": "busy"}}), "lite": ok})
    r = await GeminiChatModel(_client(fake), ["big", "lite"]).complete("sys", [Turn("elder", "hi")], AgentReply)
    assert fake.tried == ["big", "lite"]
    assert r.quickReplies == ["a", "b", "c"]  # trimmed to 3


@pytest.mark.anyio
async def test_does_not_fall_back_on_non_capacity_errors() -> None:
    from google.genai import errors

    from app.agent.llm import GeminiChatModel
    fake = _FakeModels({"big": errors.ClientError(400, {"error": {"message": "bad"}}), "lite": "{}"})
    with pytest.raises(errors.ClientError):
        await GeminiChatModel(_client(fake), ["big", "lite"]).complete("sys", [Turn("elder", "hi")], AgentReply)
    assert fake.tried == ["big"]


def _agent_reply(ws: Any) -> dict[str, Any]:
    """Read frames until the agent's message; return it."""
    while True:
        f = ws.receive_json()
        if f["type"] == "error":
            raise AssertionError(f)
        if f["type"] == "message" and f["message"]["sender"] == "agent":
            return f["message"]


def test_guided_checkin_collects_all_fields_then_logs(store: Store) -> None:
    model = FakeModel(extracted=[
        {"mood": "good"},                        # turn 1: mood from the tapped face
        {"sleepQuality": "poorly"},              # turn 2
        {"meals": "tea and toast"},              # turn 3
        {"medicationTaken": True},               # turn 4 → complete
    ])
    app.dependency_overrides[get_chat_model] = lambda: model
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            ws.send_json({"type": "auth", "token": "elder-token"})
            ws.receive_json(), ws.receive_json(), ws.receive_json()
            ws.send_json({"type": "start_checkin", "mood": "good"})
            assert _agent_reply(ws)["quickReplies"] == ["a", "b", "c"]
            for text in ["badly", "tea and toast", "yes I took it"]:
                ws.send_json({"type": "message", "text": text})
                _agent_reply(ws)
            final = ws.receive_json()
            assert final["type"] == "checkin" and final["checkin"]["status"] == "done"
    finally:
        app.dependency_overrides.clear()

    c = next(iter(store.checkins.values()))
    assert (c.status, c.mood, c.sleepQuality, c.meals, c.medicationTaken) == ("done", "good", "poorly", "tea and toast", True)
    assert [m.text for m in store["e1"] if m.sender == "elder"][0] == "आज मूड अच्छा है"  # the test elder speaks Hindi
    assert len(store.logs) == 1 and store.logs[0].kind == "warn"  # slept poorly
    assert "Elder: badly" in store.transcripts[c.id]
    assert all("daily check-in" in prompt for prompt, _ in model.calls)


def test_tap_checkin_records_the_answers_without_the_model(store: Store, model: FakeModel) -> None:
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        ws.send_json({"type": "auth", "token": "elder-token"})
        ws.receive_json(), ws.receive_json(), ws.receive_json()
        ws.send_json({"type": "checkin_answers", "mood": "low", "sleepQuality": "poorly", "ate": False, "medicationTaken": True})
        thanks = _agent_reply(ws)
        final = ws.receive_json()
    assert final["type"] == "checkin" and final["checkin"]["status"] == "done"
    c = next(iter(store.checkins.values()))
    assert (c.mood, c.sleepQuality, c.meals, c.medicationTaken) == ("low", "poorly", "nothing yet", True)
    assert len(store.logs) == 1 and store.logs[0].kind == "warn"   # low mood, poor sleep: same triage as the chat
    assert "Emma" in thanks["text"] and "तबीयत" in thanks["text"]   # Hindi elder, low mood → the gentle version
    assert model.calls == []                                        # no model needed


def test_checkin_cannot_be_started_twice_in_a_day(store: Store, model: FakeModel) -> None:
    model.extracted = [{"mood": "good", "sleepQuality": "well", "meals": "rice", "medicationTaken": True}]
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        ws.send_json({"type": "auth", "token": "elder-token"})
        ws.receive_json(), ws.receive_json(), ws.receive_json()
        ws.send_json({"type": "start_checkin", "mood": "great"})
        _agent_reply(ws)
        ws.receive_json()  # checkin state
        ws.send_json({"type": "start_checkin"})
        assert ws.receive_json()["type"] == "error"


def test_messages_during_checkin_use_checkin_prompt_then_chat_after(store: Store, model: FakeModel) -> None:
    model.extracted = [{"mood": "okay"}, {"sleepQuality": "well", "meals": "dal", "medicationTaken": False}]
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        ws.send_json({"type": "auth", "token": "elder-token"})
        ws.receive_json(), ws.receive_json(), ws.receive_json()
        ws.send_json({"type": "start_checkin"})
        _agent_reply(ws), ws.receive_json()
        ws.send_json({"type": "message", "text": "slept well, had dal, forgot my pill"})
        _agent_reply(ws), ws.receive_json()
        ws.send_json({"type": "message", "text": "what's the weather?"})
        _agent_reply(ws)
    prompts = [p for p, _ in model.calls]
    assert "daily check-in" in prompts[0] and "daily check-in" in prompts[1]
    assert "daily check-in" not in prompts[2]
    assert store.logs[0].kind == "warn"  # medicine not taken



def _connect(ws: Any) -> None:
    ws.send_json({"type": "auth", "token": "elder-token"})
    ws.receive_json(), ws.receive_json(), ws.receive_json()


def _with_model(m: FakeModel) -> None:
    app.dependency_overrides[get_chat_model] = lambda: m


def test_order_within_limit_asks_elder_then_tap_yes_places_it(store: Store) -> None:
    m = FakeModel(tool_calls=[("order_food", {"items": ["dal", "2 rotis"]})])
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "order dal and two rotis"})
            msg = _agent_reply(ws)
            card = msg["action"]
            assert m.tool_results[0]["status"] == "awaiting_elder_confirmation"
            assert card["status"] == "awaiting_elder" and card["provider"] == "Swiggy" and card["amount"] == "₹200"
            ws.send_json({"type": "confirm", "actionId": card["id"], "approve": True})
            frames = [ws.receive_json() for _ in range(3)]  # typing, action_update, narrated message
    finally:
        app.dependency_overrides.clear()
    update = next(f for f in frames if f["type"] == "action_update")
    assert update["action"]["status"] == "done" and update["action"]["orderId"].startswith("SWIGGY-")
    assert any(f["type"] == "message" and f["message"]["sender"] == "agent" for f in frames)
    assert any(l.message.startswith("Done — Food order") for l in store.logs)


def test_elder_sees_which_action_is_running_before_the_reply(store: Store) -> None:
    _with_model(FakeModel(tool_calls=[("check_weather", {})]))
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "will it rain?"})
            frames = []
            while not (frames and frames[-1]["type"] == "message" and frames[-1]["message"]["sender"] == "agent"):
                frames.append(ws.receive_json())
    finally:
        app.dependency_overrides.clear()
    typing = [f for f in frames if f["type"] == "typing"]
    assert typing[0] == {"type": "typing"} and typing[-1] == {"type": "typing", "tool": "check_weather"}


def test_tap_no_cancels_and_cannot_be_reused(store: Store) -> None:
    _with_model(FakeModel(tool_calls=[("order_food", {"items": ["idli"]})]))
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "get me idli"})
            aid = _agent_reply(ws)["action"]["id"]
            ws.send_json({"type": "confirm", "actionId": aid, "approve": False})
            [ws.receive_json() for _ in range(3)]
            ws.send_json({"type": "confirm", "actionId": aid, "approve": True})
            frames = [ws.receive_json() for _ in range(2)]
    finally:
        app.dependency_overrides.clear()
    assert store.actions[aid].status == "cancelled"
    assert frames[-1]["type"] == "error"


def test_over_limit_goes_to_caretaker_even_when_autonomous(store: Store) -> None:
    d = store.rules.model_dump()
    d["tools"]["order_food"] = {"mode": "autonomous", "limitMinor": 10_000}  # ₹100
    store.rules = type(store.rules).model_validate(d)
    m = FakeModel(tool_calls=[("order_food", {"items": ["dal", "2 rotis"]})])  # ₹200
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "order dal"})
            assert _agent_reply(ws)["action"]["status"] == "awaiting_caretaker"
    finally:
        app.dependency_overrides.clear()
    assert m.tool_results[0]["status"] == "sent_to_family_for_approval"
    assert "above the spending limit" in store.logs[-1].message and store.logs[-1].kind == "warn"


def test_blocked_tool_refuses_and_alerts_family(store: Store) -> None:
    d = store.rules.model_dump()
    d["tools"]["book_ride"] = {"mode": "blocked", "limitMinor": 0}
    store.rules = type(store.rules).model_validate(d)
    m = FakeModel(tool_calls=[("book_ride", {"destination": "the casino"})])
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "take me to the casino"})
            _agent_reply(ws)
    finally:
        app.dependency_overrides.clear()
    assert m.tool_results[0]["status"] == "not_allowed"
    assert store.logs[-1].kind == "alert"


def test_saying_yes_answers_pending_confirmation_and_tool_is_only_offered_then(store: Store) -> None:
    m = FakeModel(tool_calls=[("order_food", {"items": ["dal"]}), ("answer_pending_confirmation", {"approve": True})])
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "order dal"})
            _agent_reply(ws)
            ws.send_json({"type": "message", "text": "yes please"})
            _agent_reply(ws)
    finally:
        app.dependency_overrides.clear()
    assert "answer_pending_confirmation" not in m.offered[0]
    assert "answer_pending_confirmation" in m.offered[1]
    assert m.tool_results[1]["status"] == "done"


def test_emergency_always_alerts_even_if_everything_blocked(store: Store) -> None:
    d = store.rules.model_dump()
    for t in d["tools"].values():
        t["mode"] = "blocked"
    store.rules = type(store.rules).model_validate(d)
    m = FakeModel(tool_calls=[("escalate_emergency", {"description": "fell in the bathroom"})])
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "I fell"})
            _agent_reply(ws)
    finally:
        app.dependency_overrides.clear()
    assert m.tool_results[0]["status"] == "family_alerted" and m.tool_results[0]["emergencyNumber"] == "112"
    assert store.alerts and store.alerts[0].source == "agent"  # same path as the SOS button
    assert store.logs[-1].kind == "alert" and "fell in the bathroom" in store.logs[-1].message



def test_ride_is_handed_off_with_a_prefilled_uber_link(store: Store) -> None:
    d = store.rules.model_dump()
    d["tools"]["book_ride"] = {"mode": "autonomous", "limitMinor": 100_000}
    store.rules = type(store.rules).model_validate(d)
    m = FakeModel(tool_calls=[("book_ride", {"destination": "Ruby Hall Clinic"})])
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "take me to Ruby Hall Clinic"})
            card = _agent_reply(ws)["action"]
    finally:
        app.dependency_overrides.clear()
    assert m.tool_results[0]["status"] == "ready_to_book"
    assert card["status"] == "handed_off" and card["estimate"] and not card["demo"]
    assert card["bookingUrl"].startswith("https://m.uber.com/ul/?action=setPickup")
    assert "pickup%5Blatitude%5D=18.520000" in card["bookingUrl"]
    assert card["bookingLabel"] == "Open Uber to book"


def test_unknown_place_asks_instead_of_booking(store: Store) -> None:
    m = FakeModel(tool_calls=[("book_ride", {"destination": "nowhere land"})])
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "take me to nowhere land"})
            assert _agent_reply(ws)["action"] is None
    finally:
        app.dependency_overrides.clear()
    assert m.tool_results[0]["status"] == "place_not_found"
    assert store.actions == {}


def test_delivery_without_address_tells_family_and_creates_nothing(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ELDER_DOC, "address", None)
    m = FakeModel(tool_calls=[("order_food", {"items": ["dal"]})])
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "order dal"})
            _agent_reply(ws)
    finally:
        app.dependency_overrides.clear()
    assert m.tool_results[0]["status"] == "no_delivery_address"
    assert store.actions == {} and "no delivery address" in store.logs[-1].message


def test_food_card_is_labelled_demo_and_shows_delivery_address(store: Store) -> None:
    _with_model(FakeModel(tool_calls=[("order_food", {"items": ["dal"]})]))
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "order dal"})
            card = _agent_reply(ws)["action"]
    finally:
        app.dependency_overrides.clear()
    assert card["demo"] is True
    assert card["deliverTo"] == "12 MG Road, Flat 3, Pune, 411001"


def test_city_level_address_is_not_used_as_pickup(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ELDER_DOC, "address", {**ELDER_DOC["address"], "precision": "city"})
    d = store.rules.model_dump()
    d["tools"]["book_ride"] = {"mode": "autonomous", "limitMinor": 100_000}
    store.rules = type(store.rules).model_validate(d)
    _with_model(FakeModel(tool_calls=[("book_ride", {"destination": "Clinic"})]))
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "taxi to the clinic"})
            url = _agent_reply(ws)["action"]["bookingUrl"]
    finally:
        app.dependency_overrides.clear()
    assert "pickup=my_location" in url and "pickup%5Blatitude%5D" not in url



CARETAKER = AuthedUser(uid="c1", email=None, provider="google.com", role="caretaker")


@pytest.fixture
def two_sockets(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    def fake_auth(token: str) -> AuthedUser:
        return {"elder-token": ELDER, "care-token": CARETAKER}[token]

    def fake_load(user: AuthedUser, elder_id: str, **_: Any) -> dict[str, Any]:
        if elder_id != "e1" or (user.role == "elder" and user.uid != "e1"):
            raise HTTPException(404)
        return ELDER_DOC

    monkeypatch.setattr(chat, "authenticate", fake_auth)
    monkeypatch.setattr(chat, "load_elder", fake_load)
    return store


def test_caretaker_gets_family_thread_only_never_agent_history(two_sockets: Store, model: FakeModel) -> None:
    two_sockets["e1"].append(Message(sender="elder", text="private chat with the agent"))
    with TestClient(app).websocket_connect("/ws/e1") as care:
        care.send_json({"type": "auth", "token": "care-token"})
        first = care.receive_json()
        assert first["type"] == "family_history"
        care.send_json({"type": "message", "text": "pretend to be the elder"})
        assert care.receive_json()["type"] == "error"
    assert model.calls == []


def test_caretaker_message_reaches_elder_live_with_reply_chips(two_sockets: Store, model: FakeModel) -> None:
    # One portal (event loop) for both sockets — like a real server; otherwise cross-loop sends hang.
    with TestClient(app) as c, c.websocket_connect("/ws/e1") as elder, c.websocket_connect("/ws/e1") as care:
        elder.send_json({"type": "auth", "token": "elder-token"})
        [elder.receive_json() for _ in range(3)]
        care.send_json({"type": "auth", "token": "care-token"})
        care.receive_json()
        care.send_json({"type": "family_message", "text": "Did you take your medicine?"})
        got = elder.receive_json()
        assert got["type"] == "family_message"
        assert got["message"]["senderName"] == "Emma"
        # Hindi elder → presets are English-only; the fake model suggests nothing, so fallback chips.
        assert [c["label"] for c in got["message"]["chips"]] == ["👍 Okay", "❤️ Love you", "⏰ Remind me later"]
        assert care.receive_json()["type"] == "family_message"  # caretaker sees their own message echoed
        elder.send_json({"type": "family_reply", "text": "Yes, I took it"})
        assert care.receive_json()["message"]["sender"] == "elder"
    assert [m.sender for m in two_sockets.family] == ["caretaker", "elder"]


def test_roles_cannot_send_each_others_frame_types(two_sockets: Store, model: FakeModel) -> None:
    with TestClient(app).websocket_connect("/ws/e1") as elder:
        elder.send_json({"type": "auth", "token": "elder-token"})
        [elder.receive_json() for _ in range(3)]
        elder.send_json({"type": "family_message", "text": "spoofing a caretaker"})
        assert elder.receive_json()["type"] == "error"
    assert two_sockets.family == []


def test_agent_tool_message_to_family_lands_in_family_thread(two_sockets: Store) -> None:
    m = FakeModel(tool_calls=[("send_message_to_caretaker", {"message": "I'm fine, don't worry"})])
    _with_model(m)
    try:
        with TestClient(app) as c, c.websocket_connect("/ws/e1") as elder, c.websocket_connect("/ws/e1") as care:
            elder.send_json({"type": "auth", "token": "elder-token"})
            [elder.receive_json() for _ in range(3)]
            care.send_json({"type": "auth", "token": "care-token"})
            care.receive_json()
            elder.send_json({"type": "message", "text": "tell Emma I'm fine"})
            got = care.receive_json()
    finally:
        app.dependency_overrides.clear()
    assert got["type"] == "family_message" and got["message"]["viaAgent"] is True
    assert got["message"]["text"] == "I'm fine, don't worry"



from app.contacts import Contact  # noqa: E402


def _approve_all(store: Store, tool: str) -> None:
    d = store.rules.model_dump()
    d["tools"][tool] = {"mode": "autonomous", "limitMinor": None}
    store.rules = type(store.rules).model_validate(d)


def test_volunteer_request_texts_best_trusted_volunteer(store: Store) -> None:
    store.people = [
        Contact(name="Anil", phone="+91 91111 11111", kind="volunteer", skills=["plumbing"], trusted=False, rating=5),
        Contact(name="Suresh", phone="+91 92222 22222", kind="volunteer", skills=["plumbing"], trusted=True),
    ]
    _approve_all(store, "notify_volunteer")
    m = FakeModel(tool_calls=[("notify_volunteer", {"skill": "plumbing", "title": "Fix leaking tap", "preferred_time": "today"})])
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "my tap is leaking"})
            _agent_reply(ws)
    finally:
        app.dependency_overrides.clear()
    assert m.tool_results[0]["volunteer"] == "Suresh"
    to, body = store.texts[0]
    assert to == "+91 92222 22222" and "Fix leaking tap" in body and "12 MG Road" in body
    req = next(iter(store.requests.values()))
    assert req.status == "notified" and req.volunteerName == "Suresh" and req.notifiedVia == ["log"]


def test_volunteer_requests_wait_for_caretaker_by_default(store: Store) -> None:
    store.people = [Contact(name="Suresh", phone="+91 92222 22222", kind="volunteer", skills=["plumbing"], trusted=True)]
    m = FakeModel(tool_calls=[("notify_volunteer", {"skill": "plumbing", "title": "Fix tap"})])
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "tap leaking"})
            _agent_reply(ws)
    finally:
        app.dependency_overrides.clear()
    assert m.tool_results[0]["status"] == "sent_to_family_for_approval"
    assert store.texts == []  # nobody texted until the family approves


def test_no_matching_volunteer_tells_family(store: Store) -> None:
    _approve_all(store, "notify_volunteer")
    m = FakeModel(tool_calls=[("notify_volunteer", {"skill": "gardening", "title": "Trim hedge"})])
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "garden is overgrown"})
            _agent_reply(ws)
    finally:
        app.dependency_overrides.clear()
    assert m.tool_results[0]["status"] == "no_volunteer_found"
    assert "add a volunteer" in store.logs[-1].message and store.texts == []


def test_call_my_son_gives_tap_to_call_card_even_with_strict_rules(store: Store) -> None:
    store.people = [Contact(name="Ravi Kumar", phone="+91 98765 43210", kind="family", relationship="son")]
    m = FakeModel(tool_calls=[("call_contact", {"who": "my son"})])
    _with_model(m)
    try:
        with TestClient(app).websocket_connect("/ws/e1") as ws:
            _connect(ws)
            ws.send_json({"type": "message", "text": "call my son"})
            card = _agent_reply(ws)["action"]
    finally:
        app.dependency_overrides.clear()
    assert card["bookingUrl"] == "tel:+919876543210"
    assert card["bookingLabel"] == "📞 Call Ravi Kumar" and card["status"] == "handed_off"
    assert "Ravi Kumar (son)" in m.calls[0][0]  # agent knows who's who
    assert "+91" not in m.calls[0][0]           # …but never sees phone numbers


@pytest.mark.anyio
async def test_hung_model_times_out_and_falls_back() -> None:
    import asyncio as aio

    from app.agent.llm import GeminiChatModel

    class Slow(_FakeModels):
        async def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            if model == "big":
                self.tried.append(model)
                await aio.sleep(5)  # hangs, no error
            return await super().generate_content(model=model, contents=contents, config=config)

    fake = Slow({"lite": '{"reply": "Hi", "quickReplies": ["a", "b", "c"]}'})
    r = await GeminiChatModel(_client(fake), ["big", "lite"], per_call_timeout_s=0.05).complete("sys", [Turn("elder", "hi")], AgentReply)
    assert fake.tried == ["big", "lite"] and r.reply == "Hi"


@pytest.mark.anyio
async def test_hung_last_model_raises_timeout() -> None:
    import asyncio as aio

    from app.agent.llm import GeminiChatModel

    class Hang(_FakeModels):
        async def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            await aio.sleep(5)

    with pytest.raises(TimeoutError):
        await GeminiChatModel(_client(Hang({})), ["only"], per_call_timeout_s=0.05).complete("sys", [Turn("elder", "hi")], AgentReply)


def test_client_leaving_before_auth_is_not_an_error(store: Any, model: FakeModel, caplog: pytest.LogCaptureFixture) -> None:
    with TestClient(app).websocket_connect("/ws/e1"):
        pass  # close immediately, like React's dev double-mount
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_prompt_forbids_converting_currencies() -> None:
    from app.agent.prompt import build_system_prompt
    p = build_system_prompt({**ELDER_DOC, "language": "th-TH"}, [], dt.datetime(2026, 10, 15, tzinfo=dt.UTC))
    assert "Never convert, translate or rename a currency" in p



@contextlib.contextmanager
def _family_pair() -> Any:
    """Elder + caretaker sockets on one event loop; always closed, even when an assertion fails."""
    with TestClient(app) as c, c.websocket_connect("/ws/e1") as elder, c.websocket_connect("/ws/e1") as care:
        elder.send_json({"type": "auth", "token": "elder-token"})
        [elder.receive_json() for _ in range(3)]
        care.send_json({"type": "auth", "token": "care-token"})
        care.receive_json()
        yield elder, care


def test_english_elder_gets_remind_me_chip_and_tapping_it_sets_a_reminder(two_sockets: Store, model: FakeModel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ELDER_DOC, "language", "en-US")
    with _family_pair() as (elder, care):
        care.send_json({"type": "family_message", "text": "Did you take your medicine?"})
        m = elder.receive_json()["message"]
        care.receive_json()
        assert [ch["label"] for ch in m["chips"]] == ["✅ Yes, I took it", "❌ Not yet", "⏰ Remind me in 30 min"]
        elder.send_json({"type": "family_action", "messageId": m["id"], "chip": 2})
        upd, note = elder.receive_json(), elder.receive_json()
        assert upd["type"] == "family_update" and upd["message"]["usedChips"] == [2]
        assert note["message"]["sender"] == "system" and "Reminder set for" in note["message"]["text"]
        elder.send_json({"type": "family_action", "messageId": m["id"], "chip": 2})
        assert elder.receive_json()["type"] == "error"  # can't run twice
    assert two_sockets.reminders[0][0] == "Take your medicine"


def test_not_yet_gives_caretaker_actions_and_only_caretaker_can_run_them(two_sockets: Store, model: FakeModel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(ELDER_DOC, "phone", "+91 90000 11111")
    with _family_pair() as (elder, care):
        elder.send_json({"type": "family_reply", "text": "❌ Not yet"})
        m = care.receive_json()["message"]
        elder.receive_json()
        labels = [ch["label"] for ch in m["chips"]]
        assert labels[0].startswith("⏰ Remind Rose") and labels[-1] == "📞 Call Rose"
        assert m["chips"][-1]["action"]["phone"] == "+91 90000 11111"
        elder.send_json({"type": "family_action", "messageId": m["id"], "chip": 0})
        assert elder.receive_json()["type"] == "error"  # chips on the elder's own message are for the family
        care.send_json({"type": "family_action", "messageId": m["id"], "chip": 0})
        assert care.receive_json()["type"] == "family_update"
    assert two_sockets.reminders and two_sockets.reminders[0][0] == "Take your medicine"


def test_quick_replies_in_the_agents_voice_are_dropped() -> None:
    from app.agent.prompt import clean_quick_replies
    got = clean_quick_replies(["I am doing well", "Are you sleeping late, Rose?", "Shall I help you relax?", "Tell me a joke"], "Rose Devi")
    assert got == ["I am doing well", "Shall I help you relax?", "Tell me a joke"]


@pytest.mark.anyio
async def test_a_stalled_request_is_hedged_and_the_first_answer_wins() -> None:
    import asyncio as aio

    from app.agent.llm import GeminiChatModel

    class StallOnce(_FakeModels):
        n = 0

        async def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
            StallOnce.n += 1
            if StallOnce.n == 1:
                await aio.sleep(5)  # first request stalls
            return await super().generate_content(model=model, contents=contents, config=config)

    fake = StallOnce({"lite": '{"reply": "Hi", "quickReplies": ["a", "b", "c"]}'})
    t0 = aio.get_running_loop().time()
    r = await GeminiChatModel(_client(fake), ["lite"], per_call_timeout_s=2, hedge_after_s=0.05).complete("sys", [Turn("elder", "hi")], AgentReply)
    assert r.reply == "Hi" and StallOnce.n == 2
    assert aio.get_running_loop().time() - t0 < 1  # didn't wait for the stalled one


@pytest.mark.anyio
async def test_hedge_does_not_fire_when_the_model_answers_quickly() -> None:
    from app.agent.llm import GeminiChatModel

    fake = _FakeModels({"lite": '{"reply": "Hi", "quickReplies": []}'})
    await GeminiChatModel(_client(fake), ["lite"], hedge_after_s=1.0).complete("sys", [Turn("elder", "hi")], AgentReply)
    assert fake.tried == ["lite"]  # exactly one request


def test_a_bad_frame_says_what_went_wrong(store: Store, model: FakeModel) -> None:
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        _connect(ws)
        ws.send_json({"type": "message", "text": "x" * 1001})
        assert "too long" in ws.receive_json()["message"]
        ws.send_json({"type": "checkin_answers", "mood": "fantastic"})
        assert ws.receive_json()["message"] == "Sorry, that didn't go through. Please try again."


def test_family_chips_are_in_english_even_for_a_hindi_speaking_elder(two_sockets: Store, model: FakeModel, monkeypatch: pytest.MonkeyPatch) -> None:
    """The elder's prompt says 'reply in Hindi'; chips for the family must still be English (any Hindi label is dropped)."""
    from app.agent.chips import ChipIdea
    from app.routers.chat import ChipIdeas
    seen: list[str] = []

    async def complete(system: str, history: list[Turn], schema: type[Any]) -> Any:
        seen.append(system)
        return ChipIdeas(reply="", ideas=[ChipIdea(label="📞 कमला जी को फ़ोन करें", kind="message"), ChipIdea(label="💬 Glad you went out!", kind="message")])
    monkeypatch.setattr(model, "complete", complete)
    with _family_pair() as (elder, care):
        elder.send_json({"type": "family_reply", "text": "Went to the park with Mrs Gupta today"})
        m = care.receive_json()["message"]
        elder.receive_json()
    assert [ch["label"] for ch in m["chips"]] == ["💬 Glad you went out!"]
    assert "LANGUAGE: these labels are for the family member" in seen[-1]
