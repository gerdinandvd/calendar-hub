"""Simple in-process request rate limiter."""

from __future__ import annotations

import threading
import time
from collections import deque

_LIMIT = 60
_WINDOW_SECONDS = 60.0


class RateLimiter:
    """Sixty requests per minute for one key, in this process."""

    def __init__(self, limit: int = _LIMIT, window: float = _WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and moment - hits[0] >= self.window:
                hits.popleft()
            hits.append(moment)
            return len(hits) <= self.limit
