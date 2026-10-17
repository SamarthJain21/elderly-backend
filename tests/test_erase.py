from __future__ import annotations

from typing import Any

import pytest

from app import erase


def test_erasing_an_elder_removes_their_sign_in_pin_and_account(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted: list[str] = []

    class Doc:
        def __init__(self, path: str) -> None:
            self.path = path

        def delete(self) -> None:
            deleted.append(self.path)

    class Col:
        def __init__(self, name: str) -> None:
            self.name = name

        def document(self, id_: str) -> Doc:
            return Doc(f"{self.name}/{id_}")

        def where(self, filter: Any) -> Any:  # noqa: A002
            return type("Q", (), {"stream": lambda _s: []})()

    class DB:
        def collection(self, name: str) -> Col:
            return Col(name)

        def recursive_delete(self, ref: Doc) -> None:
            deleted.append(ref.path + "/**")

    monkeypatch.setattr(erase.firebase, "get_db", lambda: DB())
    gone: list[str] = []
    monkeypatch.setattr(erase, "_delete_auth_user", gone.append)
    erase.erase_elder("e1")
    assert {"elder_pins/e1", "rules/e1", "users/e1", "elders/e1/**"} <= set(deleted)
    assert gone == ["e1"]
