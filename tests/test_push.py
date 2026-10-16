from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from firebase_admin import messaging

from app import firebase, push


class Doc:
    def __init__(self, store: dict[str, Any], key: str) -> None:
        self.s, self.k = store, key

    def get(self) -> "Doc":
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.s.get(self.k, {})

    def update(self, d: dict[str, Any]) -> None:
        cur = self.s.setdefault(self.k, {}).setdefault("pushTokens", [])
        v = d["pushTokens"]
        if type(v).__name__ == "ArrayUnion":
            cur += [x for x in v.values if x not in cur]
        else:
            self.s[self.k]["pushTokens"] = [x for x in cur if x not in v.values]


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store: dict[str, Any] = {"c1": {"pushTokens": ["tok-good", "tok-dead"]}, "c2": {}}
    monkeypatch.setattr(firebase, "get_db", lambda: SimpleNamespace(collection=lambda _: SimpleNamespace(document=lambda k: Doc(store, k))))
    return store


def test_sends_to_every_family_device_and_drops_dead_ones(db: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[messaging.MulticastMessage] = []

    def fake_send(msg: messaging.MulticastMessage, app: Any = None) -> Any:
        sent.append(msg)
        dead = messaging.UnregisteredError("gone", cause=None, http_response=None)
        return SimpleNamespace(success_count=1, responses=[SimpleNamespace(success=True, exception=None), SimpleNamespace(success=False, exception=dead)])
    monkeypatch.setattr(messaging, "send_each_for_multicast", fake_send)
    monkeypatch.setattr(firebase, "get_app", lambda: None)
    n = push._send({"caretakerIds": ["c1", "c2"], "name": "Rose"}, "🚨 Rose needs help", "Pressed SOS", "/caretaker")
    assert n == 1 and sent[0].tokens == ["tok-good", "tok-dead"] and sent[0].notification.title == "🚨 Rose needs help"
    assert sent[0].webpush.fcm_options.link.endswith("/caretaker")
    assert db["c1"]["pushTokens"] == ["tok-good"]


def test_no_devices_no_send(db: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(messaging, "send_each_for_multicast", lambda *a, **k: pytest.fail("should not send"))
    assert push._send({"caretakerIds": ["c2"]}, "t", "b", "/") == 0


def test_add_token_keeps_at_most_ten_devices(db: dict[str, Any]) -> None:
    db["c2"] = {"pushTokens": [f"t{i}" for i in range(10)]}
    push.add_token("c2", "new")
    assert len(db["c2"]["pushTokens"]) == 10 and db["c2"]["pushTokens"][-1] == "new" and "t0" not in db["c2"]["pushTokens"]
