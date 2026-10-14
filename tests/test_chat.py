from __future__ import annotations

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
from app.checkin import CheckIn, Extracted
from app.checkin_store import LogEntry
from app.schedule import Reminder

ELDER = AuthedUser(uid="e1", email=None, provider="password", role="elder")
ELDER_DOC = {
    "name": "Rose Devi", "language": "hi-IN", "timezone": "Asia/Kolkata", "caretakerIds": ["c1"],
    "medicationSchedule": [{"name": "Amlodipine", "dose": "5mg", "times": ["08:00"], "withFood": False}],
}


class FakeModel:
    def __init__(self, fail: bool = False, extracted: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, list[Turn]]] = []
        self.fail = fail
        self.extracted = list(extracted or [])  # one per check-in turn

    async def complete(self, system: str, history: list[Turn], schema: type[Any]) -> Any:
        self.calls.append((system, history))
        if self.fail:
            raise RuntimeError("model down")
        if schema is CheckInTurn:
            return CheckInTurn(reply="Thanks!", quickReplies=["a", "b", "c"], extracted=Extracted(**self.extracted.pop(0)))
        return schema(reply="नमस्ते!", quickReplies=["अच्छा", "ठीक हूँ", "धन्यवाद"])


class Store(dict[str, list[Message]]):
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
            ws.receive_json(), ws.receive_json()
            ws.send_json({"type": "message", "text": "hi"})
            ws.receive_json(), ws.receive_json()
            assert ws.receive_json()["type"] == "error"
            ws.send_json({"type": "message", "text": "still there?"})
            assert ws.receive_json()["type"] == "message"
    finally:
        app.dependency_overrides.clear()
    assert [m.sender for m in store["e1"]] == ["elder", "elder"]  # no fake agent message saved


def test_rejects_empty_and_oversized_messages(store: Any, model: FakeModel) -> None:
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        ws.send_json({"type": "auth", "token": "elder-token"})
        ws.receive_json(), ws.receive_json()
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
        ws.receive_json()
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
            ws.receive_json(), ws.receive_json()
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
    assert [m.text for m in store["e1"] if m.sender == "elder"][0] == "I'm feeling good"
    assert len(store.logs) == 1 and store.logs[0].kind == "warn"  # slept poorly
    assert "Elder: badly" in store.transcripts[c.id]
    assert all("daily check-in" in prompt for prompt, _ in model.calls)


def test_checkin_cannot_be_started_twice_in_a_day(store: Store, model: FakeModel) -> None:
    model.extracted = [{"mood": "good", "sleepQuality": "well", "meals": "rice", "medicationTaken": True}]
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        ws.send_json({"type": "auth", "token": "elder-token"})
        ws.receive_json(), ws.receive_json()
        ws.send_json({"type": "start_checkin", "mood": "great"})
        _agent_reply(ws)
        ws.receive_json()  # checkin state
        ws.send_json({"type": "start_checkin"})
        assert ws.receive_json()["type"] == "error"


def test_messages_during_checkin_use_checkin_prompt_then_chat_after(store: Store, model: FakeModel) -> None:
    model.extracted = [{"mood": "okay"}, {"sleepQuality": "well", "meals": "dal", "medicationTaken": False}]
    with TestClient(app).websocket_connect("/ws/e1") as ws:
        ws.send_json({"type": "auth", "token": "elder-token"})
        ws.receive_json(), ws.receive_json()
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
