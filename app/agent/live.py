"""Open chat sockets per elder, so events from elsewhere (caretaker approvals) reach the elder live.

In-process only: fine for one Cloud Run instance. With several instances, a Firestore listener on
elders/{id}/messages would replace this; clients already reload history on reconnect, so nothing is lost.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, Literal

log = logging.getLogger(__name__)
Send = Callable[[dict[str, Any]], Awaitable[None]]
# "agent" = the elder's conversation with Elderly (elder's own sockets only — caretakers need
# full_access to see it); "family" = the elder ↔ caretaker thread (elder + all their caretakers).
Channel = Literal["agent", "family"]
_sockets: dict[str, dict[Send, frozenset[Channel]]] = defaultdict(dict)


def register(elder_id: str, send: Send, channels: frozenset[Channel] = frozenset({"agent", "family"})) -> None:
    _sockets[elder_id][send] = channels


def unregister(elder_id: str, send: Send) -> None:
    _sockets[elder_id].pop(send, None)
    if not _sockets[elder_id]:
        del _sockets[elder_id]


async def broadcast(elder_id: str, frame: dict[str, Any], channel: Channel = "agent") -> None:
    for send, channels in list(_sockets.get(elder_id, {}).items()):
        if channel not in channels:
            continue
        try:
            await send(frame)
        except Exception:  # noqa: BLE001 — a dead socket shouldn't break the others
            log.debug("dropping dead socket for %s", elder_id)
            unregister(elder_id, send)
