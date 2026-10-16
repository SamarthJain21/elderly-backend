"""Push notifications to the family's phones (Firebase Cloud Messaging) for the moments that matter: SOS, a worrying
check-in, an approval waiting, a message from the elder. Never raises — a failed push must not affect the elder."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from firebase_admin import messaging
from google.cloud.firestore import ArrayRemove, ArrayUnion

from app import firebase
from app.config import get_settings

log = logging.getLogger(__name__)
MAX_TOKENS = 10  # devices per family member
_tasks: set[asyncio.Task[None]] = set()


def add_token(uid: str, token: str) -> None:
    ref = firebase.get_db().collection("caretakers").document(uid)
    tokens = (ref.get().to_dict() or {}).get("pushTokens", [])
    if token in tokens:
        return
    if len(tokens) >= MAX_TOKENS:  # drop the oldest device
        ref.update({"pushTokens": ArrayRemove(tokens[: len(tokens) - MAX_TOKENS + 1])})
    ref.update({"pushTokens": ArrayUnion([token])})


def remove_token(uid: str, token: str) -> None:
    firebase.get_db().collection("caretakers").document(uid).update({"pushTokens": ArrayRemove([token])})


def _send(elder: dict[str, Any], title: str, body: str, path: str) -> int:
    db = firebase.get_db()
    owners: dict[str, str] = {}  # token → caretaker uid (to prune dead tokens)
    for uid in elder.get("caretakerIds", []):
        for t in (db.collection("caretakers").document(uid).get().to_dict() or {}).get("pushTokens", []):
            owners[t] = uid
    if not owners:
        return 0
    link = get_settings().app_url.rstrip("/") + path
    msg = messaging.MulticastMessage(
        tokens=list(owners), notification=messaging.Notification(title=title, body=body[:240]),
        webpush=messaging.WebpushConfig(fcm_options=messaging.WebpushFCMOptions(link=link)),
    )
    res = messaging.send_each_for_multicast(msg, app=firebase.get_app())
    for token, r in zip(owners, res.responses):
        if not r.success and isinstance(r.exception, (messaging.UnregisteredError, messaging.SenderIdMismatchError)):
            remove_token(owners[token], token)  # app uninstalled / permission revoked
    return res.success_count


def notify_family(elder: dict[str, Any], title: str, body: str, path: str = "/caretaker") -> None:
    """Fire and forget from async code."""
    if elder.get("sandbox"):
        return  # the live demo's pretend family has no phones
    async def go() -> None:
        try:
            await asyncio.to_thread(_send, elder, title, body, path)
        except Exception:  # noqa: BLE001
            log.warning("push failed", exc_info=True)
    task = asyncio.create_task(go())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
