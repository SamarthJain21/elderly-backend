from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import firebase
from app.agent import live
from app.autonomy import default_rules
from app.main import app
from app.routers import settings


def test_switching_a_feature_off_reaches_the_elders_open_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(firebase, "verify_id_token", lambda t: {"uid": "c1", "firebase": {"sign_in_provider": "google.com"}})
    monkeypatch.setattr(firebase, "get_user_doc", lambda uid: {"role": "caretaker"})
    monkeypatch.setattr(settings, "load_elder", lambda *a, **k: {"name": "Rose"})
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(firebase, "get_db", lambda: SimpleNamespace(collection=lambda _: SimpleNamespace(
        document=lambda _: SimpleNamespace(set=lambda d, merge: saved.append(d)))))
    frames: list[dict[str, Any]] = []

    async def push(f: dict[str, Any]) -> None:
        frames.append(f)
    live.register("e1", push, frozenset({"agent"}))
    try:
        rules = default_rules("INR").model_dump(mode="json")
        rules["tools"]["book_ride"]["mode"] = "blocked"
        r = TestClient(app).put("/elders/e1/rules", headers={"Authorization": "Bearer t"}, json=rules)
    finally:
        live.unregister("e1", push)
    assert r.status_code == 200 and saved[0]["tools"]["book_ride"]["mode"] == "blocked"
    assert frames[0]["type"] == "rules" and frames[0]["rules"]["tools"]["book_ride"]["mode"] == "blocked"
