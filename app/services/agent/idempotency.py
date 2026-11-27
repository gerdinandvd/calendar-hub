"""In-process Idempotency-Key store for agent creates."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class IdempotencyRecord:
    event_id: str
    expires_at: float


class IdempotencyStore:
    """Maps (token_id, key) → created event id for one hub process."""

    def __init__(self, ttl: float = _TTL_SECONDS) -> None:
        self.ttl = ttl
        self._items: dict[str, IdempotencyRecord] = {}
        self._lock = threading.Lock()

    def lookup(self, token_id: str, key: str) -> str | None:
        composite = _composite(token_id, key)
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            record = self._items.get(composite)
            if record is None:
                return None
            return record.event_id

    def remember(self, token_id: str, key: str, event_id: str) -> None:
        composite = _composite(token_id, key)
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            self._items[composite] = IdempotencyRecord(
                event_id=event_id, expires_at=now + self.ttl
            )

    def _purge_locked(self, now: float) -> None:
        expired = [key for key, row in self._items.items() if row.expires_at <= now]
        for key in expired:
            del self._items[key]


def _composite(token_id: str, key: str) -> str:
    return f"{token_id}\0{key}"
