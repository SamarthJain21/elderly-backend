"""In-process sliding-window rate limits. Fine while Cloud Run runs one instance (max-instances 1, which the live
registry needs anyway); move to Redis/Firestore counters if that changes. SOS is never rate-limited."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, status


class RateLimiter:
    def __init__(self, limit: int, window_s: float, clock=time.monotonic) -> None:
        self.limit, self.window_s, self._clock = limit, window_s, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        q = self._hits[key]
        while q and now - q[0] >= self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def check(self, key: str) -> None:
        """REST helper: 429 when over the limit."""
        if not self.allow(key):
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests — please wait a little and try again")


# Each chat frame can cost a Gemini call. 20/min is far above a real conversation.
chat_frames = RateLimiter(20, 60)
# Account-creating / email-sending endpoints.
account_writes = RateLimiter(10, 3600)
# 'Try with real AI' sandboxes (no sign-in): per visitor IP, and a daily cap on the whole thing.
sandbox_per_ip = RateLimiter(20, 3600)  # judges press "Start over"; offices share one IP
sandbox_global = RateLimiter(300, 86400)
# Read-aloud clips: one per reply/message, plus replays.
tts_calls = RateLimiter(60, 60)
