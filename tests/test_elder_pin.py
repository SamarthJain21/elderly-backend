from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import elder_pin
from app.auth import AuthedUser, get_authed_user
from app.main import app
from app.routers import elder_access

ELDER = AuthedUser(uid="e1", email=None, provider="password", role="elder")
CARETAKER = AuthedUser(uid="c1", email=None, provider="google.com", role="caretaker")
PHONE = "+91 98765 43210"


class Ref:
    def __init__(self, store: dict[str, dict[str, Any]], id_: str) -> None:
        self.store, self.id = store, id_

    def set(self, d: dict[str, Any]) -> None:
        self.store[self.id] = {k: (None if k == "updatedAt" else v) for k, v in d.items()}

    def update(self, d: dict[str, Any]) -> None:
        self.store[self.id].update(d)

    def get(self) -> Any:
        return Snap(self)


class Snap:
    def __init__(self, ref: Ref) -> None:
        self.reference, self.id = ref, ref.id
        self.exists = ref.id in ref.store

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self.reference.store[self.id]) if self.exists else None


class Col:
    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}

    def document(self, id_: str) -> Ref:
        return Ref(self.store, id_)

    def where(self, filter: Any) -> Any:  # noqa: A002 — Firestore's own keyword
        want = filter.value if isinstance(filter.value, list) else [filter.value]
        return type("Q", (), {"stream": lambda _self: [Snap(Ref(self.store, k)) for k, v in self.store.items() if v["phoneKey"] in want]})()


@pytest.fixture
def pins(monkeypatch: pytest.MonkeyPatch) -> Col:
    col = Col()
    monkeypatch.setattr(elder_pin, "_col", lambda: col)
    return col


def test_phone_formats_match_and_the_right_pin_signs_in(pins: Col) -> None:
    elder_pin.set_pin("e1", PHONE, "4826")
    assert "4826" not in str(pins.store["e1"])                     # only a salted hash is kept
    assert elder_pin.check("+919876543210", "4826") == "e1"
    assert elder_pin.check(PHONE, "4827") is None


def test_a_shared_phone_finds_the_person_whose_pin_it_is(pins: Col) -> None:
    elder_pin.set_pin("kamla", PHONE, "4826")
    elder_pin.set_pin("ramesh", PHONE, "7391")
    assert elder_pin.check(PHONE, "7391") == "ramesh"
    assert elder_pin.check(PHONE, "4826") == "kamla"


def test_five_wrong_tries_lock_the_pin_for_a_while(pins: Col) -> None:
    elder_pin.set_pin("e1", PHONE, "4826")
    now = dt.datetime(2026, 10, 17, 9, 0, tzinfo=dt.UTC)
    for _ in range(4):
        assert elder_pin.check(PHONE, "0000", now) is None
    with pytest.raises(elder_pin.Locked):
        elder_pin.check(PHONE, "0000", now)
    with pytest.raises(elder_pin.Locked):                           # even the right PIN waits
        elder_pin.check(PHONE, "4826", now + dt.timedelta(minutes=5))
    assert elder_pin.check(PHONE, "4826", now + dt.timedelta(minutes=16)) == "e1"
    assert pins.store["e1"]["failed"] == 0


@pytest.mark.parametrize("bad", ["123", "1234567", "12a4", "1111", "1234", "9876"])
def test_weak_or_malformed_pins_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        elder_pin._pin(bad)


# ---------- endpoints ----------

@pytest.fixture
def client(pins: Col, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"user": ELDER, "updated": [], "revoked": []}
    monkeypatch.setattr(elder_access, "load_elder", lambda user, eid, **kw: (
        (_ for _ in ()).throw(elder_access.HTTPException(403, "no")) if kw.get("caretaker_only") and user.role != "caretaker"
        else {"phone": PHONE}))
    monkeypatch.setattr(elder_access.fb_auth, "update_user",
                        lambda uid, password, app: state["updated"].append(password) or type("R", (), {"email": "elder-x@elderly.app"})())
    monkeypatch.setattr(elder_access.fb_auth, "revoke_refresh_tokens", lambda uid, app: state["revoked"].append(uid))
    monkeypatch.setattr(elder_access.firebase, "get_app", lambda: None)
    monkeypatch.setattr(elder_pin, "custom_token", lambda eid: f"token-for-{eid}")
    app.dependency_overrides[get_authed_user] = lambda: state["user"]
    state["c"] = TestClient(app)
    yield state
    app.dependency_overrides.clear()


def test_elder_sets_a_pin_then_signs_in_with_phone_and_pin(client: dict[str, Any]) -> None:
    c = client["c"]
    assert c.get("/elders/e1/pin").json() == {"set": False}
    assert c.put("/elders/e1/pin", json={"pin": "1234"}).status_code == 422
    assert c.put("/elders/e1/pin", json={"pin": "4826"}).json() == {"set": True}
    assert c.post("/auth/pin", json={"phone": "+919876543210", "pin": "4826"}).json() == {"token": "token-for-e1"}
    r = c.post("/auth/pin", json={"phone": PHONE, "pin": "0000"})
    assert r.status_code == 401 and "don't match" in r.json()["detail"]


def test_new_setup_link_is_family_only_and_can_sign_out_other_phones(client: dict[str, Any]) -> None:
    c = client["c"]
    assert c.post("/elders/e1/setup-link", json={}).status_code == 403          # the elder can't
    client["user"] = CARETAKER
    r = c.post("/elders/e1/setup-link", json={"signOutOthers": True})
    assert r.status_code == 200
    assert r.json()["tempPassword"] == client["updated"][0] and r.json()["email"] == "elder-x@elderly.app"
    assert client["revoked"] == ["e1"]


def test_a_number_saved_without_country_code_still_signs_in_with_it(pins: Col) -> None:
    elder_pin.set_pin("varuna", "9592812463", "4826")              # stored before the + rule
    assert elder_pin.check("+91 95928 12463", "4826") == "varuna"
