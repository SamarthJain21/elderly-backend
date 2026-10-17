from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth as fb_auth

from app import firebase
from app.main import app
from app.routers.elders import _PASSWORD_ALPHABET, generate_temp_password

# token -> (claims, users/{uid} doc)
TOKENS: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] = {
    "caretaker": ({"uid": "c1", "email": "c@x.com", "firebase": {"sign_in_provider": "google.com"}},
                  {"role": "caretaker"}),
    "elder": ({"uid": "e1", "email": "e@elderly.app", "firebase": {"sign_in_provider": "password"}},
              {"role": "elder"}),
    "fake-caretaker": ({"uid": "c2", "firebase": {"sign_in_provider": "password"}},
                       {"role": "caretaker"}),
    "new-google": ({"uid": "n1", "email": "n@x.com", "firebase": {"sign_in_provider": "google.com"}},
                   None),
    "new-password": ({"uid": "n2", "firebase": {"sign_in_provider": "password"}}, None),
}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    def fake_verify(token: str) -> dict[str, Any]:
        if token not in TOKENS:
            raise fb_auth.InvalidIdTokenError("bad token")
        return TOKENS[token][0]

    docs = {claims["uid"]: doc for claims, doc in TOKENS.values()}
    monkeypatch.setattr(firebase, "verify_id_token", fake_verify)
    monkeypatch.setattr(firebase, "get_user_doc", lambda uid: docs[uid])
    return TestClient(app)


def h(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_me_without_token_is_401(client: TestClient) -> None:
    assert client.get("/me").status_code == 401


def test_me_with_invalid_token_is_401(client: TestClient) -> None:
    assert client.get("/me", headers=h("garbage")).status_code == 401


def test_me_returns_role_for_onboarded_user(client: TestClient) -> None:
    r = client.get("/me", headers=h("elder"))
    assert r.status_code == 200
    assert r.json() == {"uid": "e1", "email": "e@elderly.app", "role": "elder", "admin": False, "tourDone": False}


def test_me_is_404_before_onboarding(client: TestClient) -> None:
    assert client.get("/me", headers=h("new-google")).status_code == 404


def test_caretaker_role_via_password_login_is_403(client: TestClient) -> None:
    assert client.get("/me", headers=h("fake-caretaker")).status_code == 403


def test_elder_cannot_create_elders(client: TestClient) -> None:
    body = {"name": "Rose", "phone": "+911234567890"}
    assert client.post("/elders", json=body, headers=h("elder")).status_code == 403


def test_password_user_cannot_self_onboard_as_caretaker(client: TestClient) -> None:
    r = client.post("/onboarding/caretaker", json={"name": "X"}, headers=h("new-password"))
    assert r.status_code == 403


def test_already_onboarded_user_cannot_onboard_again(client: TestClient) -> None:
    r = client.post("/onboarding/caretaker", json={"name": "X"}, headers=h("elder"))
    assert r.status_code == 409


def test_temp_password_uses_only_unambiguous_chars() -> None:
    pw = generate_temp_password()
    assert len(pw) == 10
    assert set(pw) <= set(_PASSWORD_ALPHABET)


def test_tour_is_remembered_for_the_account_not_the_browser(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.get("/me", headers=h("elder")).json()["tourDone"] is False
    writes: list[dict[str, Any]] = []
    doc_ref = type("Ref", (), {"update": lambda self, d: writes.append(d)})()
    db = type("Db", (), {"collection": lambda self, _c: type("Col", (), {"document": lambda self, _u: doc_ref})()})()
    monkeypatch.setattr(firebase, "get_db", lambda: db)
    assert client.post("/me/tour-done", headers=h("elder")).status_code == 204
    assert list(writes[0]) == ["tourDoneAt"]

    monkeypatch.setattr(firebase, "get_user_doc", lambda uid: {"role": "elder", "tourDoneAt": "2026-10-17"})
    assert client.get("/me", headers=h("elder")).json()["tourDone"] is True
    assert client.post("/me/tour-done", headers=h("elder")).status_code == 204 and len(writes) == 1  # no rewrite


def test_tour_done_needs_an_onboarded_account(client: TestClient) -> None:
    assert client.post("/me/tour-done", headers=h("new-google")).status_code == 404


def test_signing_up_skips_an_invite_to_an_elder_that_no_longer_exists(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[tuple[str, Any]] = []

    class Ref:
        def __init__(self, path: str) -> None:
            self.path, self.id = path, path.split("/")[-1]

        def get(self) -> Any:
            data = {"elderIds": ["gone", "alive"]} if self.path == "invites/n@x.com" else None
            return type("Snap", (), {"to_dict": lambda _s: data, "exists": data is not None})()

    class Batch:
        def set(self, ref: Ref, d: Any) -> None: writes.append(("set", ref.path, d))  # noqa: E704
        def update(self, ref: Ref, d: Any) -> None: writes.append(("update", ref.path))  # noqa: E704
        def delete(self, ref: Ref) -> None: writes.append(("delete", ref.path))  # noqa: E704
        def commit(self) -> None: pass  # noqa: E704

    class DB:
        def collection(self, name: str) -> Any:
            return type("C", (), {"document": lambda _s, i: Ref(f"{name}/{i}")})()

        def batch(self) -> Batch:
            return Batch()

        def get_all(self, refs: list[Ref]) -> list[Any]:
            return [type("S", (), {"id": r.id, "exists": r.id == "alive"})() for r in refs]

    monkeypatch.setattr(firebase, "get_db", lambda: DB())
    r = client.post("/onboarding/caretaker", json={"name": "Nok"}, headers=h("new-google"))
    assert r.status_code == 201
    assert ("update", "elders/alive") in writes and ("update", "elders/gone") not in writes
    caretaker = next(w for w in writes if w[0] == "set" and w[1] == "caretakers/n1")
    assert caretaker[2]["elderIds"] == ["alive"]
