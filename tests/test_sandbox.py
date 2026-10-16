from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app import firebase, sandbox
from app.main import app
from app.sms import send_sms

SANDBOX_CT = ({"uid": "s1", "email": "sandbox-family-x@elderly.app", "firebase": {"sign_in_provider": "password"}},
              {"role": "caretaker", "sandbox": True})
FAKE_CT = ({"uid": "c2", "firebase": {"sign_in_provider": "password"}}, {"role": "caretaker"})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    tokens = {"sandbox-ct": SANDBOX_CT, "fake-ct": FAKE_CT}

    def verify(token: str) -> dict[str, Any]:
        if token not in tokens:
            raise fb_auth.InvalidIdTokenError("bad")
        return tokens[token][0]
    monkeypatch.setattr(firebase, "verify_id_token", verify)
    monkeypatch.setattr(firebase, "get_user_doc", lambda uid: {c["uid"]: d for c, d in tokens.values()}[uid])
    return TestClient(app)


def h(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


def test_sandbox_caretaker_may_use_a_password_but_a_normal_caretaker_may_not(client: TestClient) -> None:
    assert client.get("/me", headers=h("sandbox-ct")).json()["role"] == "caretaker"
    assert client.get("/me", headers=h("fake-ct")).status_code == 403


def test_sandbox_caretaker_cannot_create_real_accounts(client: TestClient) -> None:
    r = client.post("/elders", headers=h("sandbox-ct"), json={"name": "X", "phone": "+91 99999 99999"})
    assert r.status_code == 403


def test_sandbox_numbers_are_never_texted(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import sms
    monkeypatch.setattr(sms.httpx, "post", lambda *a, **k: pytest.fail("tried to send"))
    assert send_sms("+000 1234567", "hi") == "log"


def test_history_tells_the_story_a_steady_fortnight_then_two_low_days() -> None:
    today = dt.date(2026, 10, 16)
    h_ = sandbox._history("e1", sandbox.PERSONAS["en-US"], today)
    assert [c.date for c in h_] == [today - dt.timedelta(days=n) for n in range(13, 0, -1)]  # today left open
    assert [c.mood for c in h_[-2:]] == ["low", "low"] and h_[-3].mood != "low"
    assert sum(c.mood in ("low", "bad") for c in h_[:-2]) == 0
    assert all(c.status == "done" and c.id == f"e1_{c.date}" for c in h_)


def test_every_language_has_a_persona() -> None:
    assert set(sandbox.PERSONAS) == {"en-US", "hi-IN", "hi-Latn", "th-TH"}


def test_endpoint_is_rate_limited_per_visitor(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[str] = []

    def fake_create(lang: str) -> sandbox.Sandbox:
        made.append(lang)
        cred = sandbox.Credentials(name="Rose", email="e@x", password="p")
        return sandbox.Sandbox(elderId="e", language=lang, elder=cred, caretaker=cred,  # type: ignore[arg-type]
                               expiresAt=dt.datetime(2026, 10, 17, tzinfo=dt.UTC))
    monkeypatch.setattr(sandbox, "create_sandbox", fake_create)
    monkeypatch.setattr(sandbox, "cleanup_expired", lambda: 0)
    ip = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
    codes = [client.post("/demo/sandbox", json={"language": "th-TH"}, headers=ip).status_code for _ in range(6)]
    assert codes == [201] * 5 + [429]
    assert client.post("/demo/sandbox", json={}, headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 201
    assert made[-1] == "en-US"


def test_cleanup_never_erases_a_real_elder(monkeypatch: pytest.MonkeyPatch) -> None:
    class Snap:
        def __init__(self, id_: str, d: dict[str, Any]) -> None:
            self.id, self._d = id_, d

        def to_dict(self) -> dict[str, Any]:
            return self._d

    class Q:
        def where(self, **_: Any) -> Q:
            return self

        def limit(self, _: int) -> Q:
            return self

        def stream(self) -> list[Snap]:
            return [Snap("real", {"sandboxExpiresAt": 1}), Snap("sb", {"sandbox": True, "sandboxCaretakerId": "c"})]

    class DB:
        def collection(self, _: str) -> Q:
            return Q()
    monkeypatch.setattr(firebase, "get_db", lambda: DB())
    erased: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(sandbox, "erase_elder", lambda eid, also_caretakers=(): erased.append((eid, list(also_caretakers))))
    assert sandbox.cleanup_expired() == 1
    assert erased == [("sb", ["c"])]


def test_checkin_is_due_when_the_visitor_arrives() -> None:
    now = dt.datetime(2026, 10, 16, 8, 52, tzinfo=dt.UTC)  # 15:52 in Bangkok
    assert sandbox.checkin_time(now, "Asia/Bangkok") == "15:45"
