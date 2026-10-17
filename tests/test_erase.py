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


def test_erasing_an_elder_clears_pending_invites_to_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leftover invite to a deleted elder made the invited person's sign-up fail."""
    changes: list[tuple[str, Any]] = []

    class Ref:
        def __init__(self, path: str) -> None:
            self.path = path

        def delete(self) -> None:
            changes.append(("delete", self.path))

        def update(self, d: dict[str, Any]) -> None:
            changes.append(("update", self.path))

    invites = {"only@x.com": ["e1"], "two@x.com": ["e1", "e2"]}

    class Col:
        def __init__(self, name: str) -> None:
            self.name = name

        def document(self, id_: str) -> Ref:
            return Ref(f"{self.name}/{id_}")

        def where(self, filter: Any) -> Any:  # noqa: A002
            if self.name != "invites":
                return type("Q", (), {"stream": lambda _s: []})()
            snaps = [type("S", (), {"reference": Ref(f"invites/{k}"), "to_dict": lambda _s, v=v: {"elderIds": v}})()
                     for k, v in invites.items() if filter.value in v]
            return type("Q", (), {"stream": lambda _s: snaps})()

    class DB:
        def collection(self, name: str) -> Col:
            return Col(name)

        def recursive_delete(self, ref: Ref) -> None:
            pass

    monkeypatch.setattr(erase.firebase, "get_db", lambda: DB())
    monkeypatch.setattr(erase, "_delete_auth_user", lambda uid: None)
    erase.erase_elder("e1")
    assert ("delete", "invites/only@x.com") in changes        # nothing else invited there: gone
    assert ("update", "invites/two@x.com") in changes         # still invited to e2: just e1 removed
