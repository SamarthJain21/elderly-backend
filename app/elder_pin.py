"""Elder sign-in PIN: phone number + 4–6 digits, checked on the server.

A short PIN can't be the Firebase password (6+ characters, and anyone could try it straight against Firebase).
So only a salted scrypt hash is kept, in elder_pins/{elderId} — a collection no client can read (no rule matches
it) — and wrong tries lock the PIN for a while. A right PIN gets a Firebase custom token for the elder's account.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import secrets
from typing import Annotated, Any

from google.cloud.firestore import SERVER_TIMESTAMP, FieldFilter
from pydantic import AfterValidator

from app import firebase

MAX_TRIES = 5
LOCK = dt.timedelta(minutes=15)


def _pin(v: str) -> str:
    if not re.fullmatch(r"\d{4,6}", v):
        raise ValueError("a PIN is 4 to 6 digits")
    if len(set(v)) == 1 or v in "0123456789" or v in "9876543210":
        raise ValueError("pick a PIN that's harder to guess than 1111 or 1234")
    return v


Pin = Annotated[str, AfterValidator(_pin)]


def phone_key(phone: str) -> str:
    """'+91 98765 43210' and '+919876543210' are the same number."""
    return "".join(ch for ch in phone if ch.isdigit())


def _hash(pin: str, salt: bytes) -> bytes:
    return hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _col():  # noqa: ANN202
    return firebase.get_db().collection("elder_pins")


def set_pin(elder_id: str, phone: str, pin: str) -> None:
    salt = secrets.token_bytes(16)
    _col().document(elder_id).set({"phoneKey": phone_key(phone), "salt": salt, "hash": _hash(pin, salt),
                                   "failed": 0, "lockedUntil": None, "updatedAt": SERVER_TIMESTAMP})


def has_pin(elder_id: str) -> bool:
    return _col().document(elder_id).get().exists


def update_phone(elder_id: str, phone: str) -> None:
    """The family changed the elder's number: the PIN now goes with the new one."""
    ref = _col().document(elder_id)
    if ref.get().exists:
        ref.update({"phoneKey": phone_key(phone)})


class Locked(Exception):
    def __init__(self, until: dt.datetime) -> None:
        self.until = until


def check(phone: str, pin: str, now: dt.datetime | None = None) -> str | None:
    """The elderId whose PIN this is, or None. Raises Locked when every account on that number is locked.

    One phone can belong to two people (a couple sharing a phone), each with their own PIN.
    """
    now = now or dt.datetime.now(dt.UTC)
    docs = list(_col().where(filter=FieldFilter("phoneKey", "==", phone_key(phone))).stream())
    locked: list[dt.datetime] = []
    for d in docs:
        data: dict[str, Any] = d.to_dict() or {}
        until = data.get("lockedUntil")
        if until and until > now:
            locked.append(until)
            continue
        if hmac.compare_digest(_hash(pin, data["salt"]), data["hash"]):
            d.reference.update({"failed": 0, "lockedUntil": None})
            return d.id
    for d in docs:  # wrong for everyone on this number: count a try against each unlocked account
        data = d.to_dict() or {}
        if (u := data.get("lockedUntil")) and u > now:
            continue
        failed = int(data.get("failed", 0)) + 1
        d.reference.update({"failed": 0 if failed >= MAX_TRIES else failed,
                            "lockedUntil": now + LOCK if failed >= MAX_TRIES else None})
        if failed >= MAX_TRIES:
            locked.append(now + LOCK)
    if docs and len(locked) == len(docs):
        raise Locked(min(locked))
    return None


def custom_token(elder_id: str) -> str:
    from firebase_admin import auth
    return auth.create_custom_token(elder_id, app=firebase.get_app()).decode()
