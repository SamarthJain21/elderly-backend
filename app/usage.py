"""What we spend on Google APIs, counted as we go: Gemini tokens, voice characters, address lookups.
Counted in memory and added to usage/{date} once a minute, so recording never slows a reply. A crash can lose up
to a minute of counts — fine for a cost dashboard (Cloud Billing stays the source of truth)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
from collections import Counter
from typing import Any

from google.cloud.firestore import Increment

from app import firebase

log = logging.getLogger(__name__)
_lock = threading.Lock()
_pending: Counter[str] = Counter()

# Estimated USD per unit, for the dashboard only. Gemini: per 1M tokens (in, out); voice: per 1M characters;
# Places: per 1000 calls. Google's official pages are the source of truth — update these when they change.
PRICES: dict[str, dict[str, float]] = {
    "gemini-flash-lite": {"in": 0.25, "out": 1.50},
    "gemini-flash": {"in": 0.75, "out": 3.75},
    "tts-chirp3-hd": {"chars": 30.0},
    "places-autocomplete": {"calls": 2.83},
    "places-details": {"calls": 5.0},
}


def _family(model: str) -> str:
    return "gemini-flash-lite" if "lite" in model else "gemini-flash"


def add(key: str, **amounts: int) -> None:
    """add('tts-chirp3-hd', chars=120). Thread-safe; never raises."""
    with _lock:
        for unit, n in amounts.items():
            if n:
                _pending[f"{key}|{unit}"] += int(n)


def gemini(model: str, response: Any, purpose: str) -> None:
    """Record a Gemini response's token counts (thinking tokens are billed as output)."""
    try:
        u = response.usage_metadata
        out = (u.candidates_token_count or 0) + (getattr(u, "thoughts_token_count", 0) or 0)
        add(_family(model), **{"in": u.prompt_token_count or 0, "out": out, "calls": 1})
        add(f"purpose:{purpose}", calls=1)
    except Exception:  # noqa: BLE001 — never let accounting break a reply
        pass


def flush() -> None:
    with _lock:
        batch, _pending_copy = dict(_pending), None
        _pending.clear()
    if not batch:
        return
    day = dt.datetime.now(dt.UTC).date().isoformat()
    try:
        firebase.get_db().collection("usage").document(day).set({k: Increment(v) for k, v in batch.items()}, merge=True)
    except Exception:  # noqa: BLE001 — put them back for the next try
        log.warning("usage flush failed", exc_info=True)
        with _lock:
            _pending.update(batch)


async def run_flusher(every_s: float = 60) -> None:
    while True:
        await asyncio.sleep(every_s)
        await asyncio.to_thread(flush)


def cost(counts: dict[str, int]) -> dict[str, float]:
    """Estimated USD per service from one day's counts."""
    out: dict[str, float] = {}
    for key, rates in PRICES.items():
        usd = 0.0
        for unit, rate in rates.items():
            n = counts.get(f"{key}|{unit}", 0)
            usd += n * rate / (1000 if key.startswith("places") else 1_000_000)
        if usd:
            out[key] = round(usd, 4)
    return out
