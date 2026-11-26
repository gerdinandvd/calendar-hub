"""Short-lived single-use delete confirmation tokens."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from app.services.agent.types import CONFIRM_TTL_SECONDS


@dataclass(frozen=True)
class ConfirmRecord:
    event_id: str
    preview: dict
    expires_at: float


class ConfirmStore:
    """In-process confirm tokens. Bound to one hub process."""

    def __init__(self, ttl: float = CONFIRM_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._items: dict[str, ConfirmRecord] = {}
        self._lock = threading.Lock()

    def issue(self, event_id: str, preview: dict) -> tuple[str, float]:
        token = secrets.token_urlsafe(24)
        expires = time.monotonic() + self.ttl
        with self._lock:
            self._purge_locked(time.monotonic())
            self._items[token] = ConfirmRecord(
                event_id=event_id, preview=preview, expires_at=expires
            )
        return token, time.time() + self.ttl

    def consume(self, token: str, event_id: str) -> ConfirmRecord | None:
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            record = self._items.pop(token, None)
        if record is None:
            return None
        if record.event_id != event_id:
            return None
        return record

    def _purge_locked(self, now: float) -> None:
        expired = [key for key, row in self._items.items() if row.expires_at <= now]
        for key in expired:
            del self._items[key]
