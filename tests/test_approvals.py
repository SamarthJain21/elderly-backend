from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent import live
from app.agent.llm import AgentReply, get_chat_model
from app.auth import AuthedUser, get_authed_user
from app.main import app
from app.routers import approvals
from app.tools.actions import Action
from app.tools.providers import quote_food

CARETAKER = AuthedUser(uid="c1", email=None, provider="google.com", role="caretaker")
ELDER = {"name": "Rose", "language": "en-US", "timezone": "Asia/Kolkata", "caretakerIds": ["c1"]}


class Narrator:
    async def complete(self, system: str, history: Any, schema: Any) -> Any:
        return AgentReply(reply="Your family said yes!", quickReplies=["Thanks", "Great", "Okay"])


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"actions": {}, "logs": [], "pushed": []}
    a = Action(elderId="e1", tool="order_food", args={"items": ["dal"]}, status="awaiting_caretaker",
               quote=quote_food(["dal"], "INR"), messageId="m1")
    state["actions"][a.id] = a
    state["id"] = a.id
    monkeypatch.setattr(approvals, "load_elder", lambda user, eid, **_: ELDER)
    monkeypatch.setattr(approvals.actions, "get", lambda _e, aid: state["actions"].get(aid))
    monkeypatch.setattr(approvals.actions, "save", lambda x: state["actions"].__setitem__(x.id, x))
    monkeypatch.setattr(approvals.checkin_store, "add_log", lambda _e, entry: state["logs"].append(entry))
    import app.agent.narrate as nar
    monkeypatch.setattr(nar.messages, "set_action_card", lambda *_: None)
    monkeypatch.setattr(nar.messages, "save", lambda *_: None)
    monkeypatch.setattr(nar.messages, "recent", lambda *_: [])

    async def push(frame: dict[str, Any]) -> None:
        state["pushed"].append(frame)

    live.register("e1", push)
    app.dependency_overrides[get_authed_user] = lambda: CARETAKER
    app.dependency_overrides[get_chat_model] = lambda: Narrator()
    yield state
    app.dependency_overrides.clear()
    live.unregister("e1", push)


def test_approve_places_order_and_tells_elder_live(env: dict[str, Any]) -> None:
    r = TestClient(app).post(f"/elders/e1/actions/{env['id']}/decide", json={"approve": True})
    assert r.status_code == 200 and r.json()["status"] == "done"
    kinds = [f["type"] for f in env["pushed"]]
    assert kinds == ["action_update", "message"]
    assert env["pushed"][1]["message"]["text"] == "Your family said yes!"


def test_decline_and_double_decide_is_409(env: dict[str, Any]) -> None:
    c = TestClient(app)
    assert c.post(f"/elders/e1/actions/{env['id']}/decide", json={"approve": False}).json()["status"] == "declined"
    assert c.post(f"/elders/e1/actions/{env['id']}/decide", json={"approve": True}).status_code == 409
    assert env["logs"][-1].message.startswith("Declined")
