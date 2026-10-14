"""Open chat sockets per elder, so events from elsewhere (caretaker approvals) reach the elder live.

In-process only: fine for one Cloud Run instance. With several instances, a Firestore listener on
elders/{id}/messages would replace this; clients already reload history on reconnect, so nothing is lost.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)
Send = Callable[[dict[str, Any]], Awaitable[None]]
_sockets: dict[str, set[Send]] = defaultdict(set)


def register(elder_id: str, send: Send) -> None:
    _sockets[elder_id].add(send)


def unregister(elder_id: str, send: Send) -> None:
    _sockets[elder_id].discard(send)
    if not _sockets[elder_id]:
        del _sockets[elder_id]


async def broadcast(elder_id: str, frame: dict[str, Any]) -> None:
    for send in list(_sockets.get(elder_id, ())):
        try:
            await send(frame)
        except Exception:  # noqa: BLE001 — a dead socket shouldn't break the others
            log.debug("dropping dead socket for %s", elder_id)
            unregister(elder_id, send)
