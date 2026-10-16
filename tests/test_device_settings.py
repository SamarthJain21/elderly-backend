from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import firebase
from app.agent import live
from app.main import app
from app.routers import device_api

ELDER: dict[str, Any] = {"name": "Rose", "caretakerIds": ["c1"]}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"saved": None, "frames": []}
    monkeypatch.setattr(firebase, "verify_id_token", lambda t: {"uid": "c1", "firebase": {"sign_in_provider": "google.com"}})
    monkeypatch.setattr(firebase, "get_user_doc", lambda uid: {"role": "caretaker"})
    monkeypatch.setattr(device_api, "load_elder", lambda user, eid: {**ELDER, "deviceSettings": state["saved"]})

    class Doc:
        def update(self, d: dict[str, Any]) -> None:
            state["saved"] = d["deviceSettings"]

    class DB:
        def collection(self, _: str) -> "DB":
            return self

        def document(self, _: str) -> Doc:
            return Doc()
    monkeypatch.setattr(firebase, "get_db", lambda: DB())

    async def push(frame: dict[str, Any]) -> None:
        state["frames"].append(frame)
    live.register("e1", push, frozenset({"agent"}))
    yield state
    live.unregister("e1", push)


H = {"Authorization": "Bearer t"}


def test_defaults_before_anyone_changes_anything(env: dict[str, Any]) -> None:
    assert TestClient(app).get("/elders/e1/device-settings", headers=H).json() == {"voice": True, "volume": 1.0, "rate": 1.0, "textSize": "normal"}


def test_family_changes_reach_the_elders_open_app(env: dict[str, Any]) -> None:
    r = TestClient(app).put("/elders/e1/device-settings", headers=H, json={"voice": True, "volume": 0.6, "rate": 0.8, "textSize": "xlarge"})
    assert r.status_code == 200 and env["saved"]["textSize"] == "xlarge"
    assert env["frames"] == [{"type": "device_settings", "settings": {"voice": True, "volume": 0.6, "rate": 0.8, "textSize": "xlarge"}}]


def test_out_of_range_values_are_rejected(env: dict[str, Any]) -> None:
    assert TestClient(app).put("/elders/e1/device-settings", headers=H, json={"volume": 3, "rate": 0.1}).status_code == 422
