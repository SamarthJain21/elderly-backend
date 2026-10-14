from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.agent.llm import AgentReply, Turn, get_chat_model
from app.agent.messages import Message
from app.agent.prompt import build_system_prompt
from app.auth import AuthedUser
from app.main import app
from app.routers import chat
from app.schedule import Reminder

ELDER = AuthedUser(uid="e1", email=None, provider="password", role="elder")
ELDER_DOC = {"name": "Rose Devi", "language": "hi-IN", "timezone": "Asia/Kolkata", "caretakerIds": ["c1"]}


class FakeModel:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, list[Turn]]] = []
        self.fail = fail

    async def reply(self, system: str, history: list[Turn]) -> AgentReply:
        self.calls.append((system, history))
        if self.fail:
            raise RuntimeError("model down")
        return AgentReply(reply="नमस्ते!", quickReplies=["अच्छा", "ठीक हूँ", "धन्यवाद"])


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Message]]:
    db: dict[str, list[Message]] = {"e1": []}

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
            ws.receive_json()
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
        ws.receive_json()
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
    assert "10:00 Doctor visit" in p
    assert "Call them Rose" in p
