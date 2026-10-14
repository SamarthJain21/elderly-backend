from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app import firebase
from app.auth import AuthedUser


def _not_found() -> HTTPException:
    # 404 (not 403) for elders you can't see, so IDs can't be probed.
    return HTTPException(status.HTTP_404_NOT_FOUND, "Elder not found")


def load_elder(user: AuthedUser, elder_id: str, *, caretaker_only: bool = False) -> dict[str, Any]:
    """Return elders/{elder_id} if `user` is that elder or one of their caretakers."""
    snap = firebase.get_db().collection("elders").document(elder_id).get()
    if not snap.exists:
        raise _not_found()
    elder: dict[str, Any] = snap.to_dict() or {}

    is_caretaker = user.role == "caretaker" and user.uid in elder.get("caretakerIds", [])
    is_self = user.role == "elder" and user.uid == elder_id
    if not (is_caretaker or is_self):
        raise _not_found()
    if caretaker_only and not is_caretaker:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a caretaker can change this")
    return elder
