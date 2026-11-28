"""Injectable system ports: clock, audit, crypto, rate limiting."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...

    def utc_stamp(self) -> str:
        """Return an ISO-8601 UTC timestamp ending in Z."""
        ...


class AuditLogger(Protocol):
    def log_mutation(self, *, actor: str, source_id: str, message: str) -> None: ...


class Cipher(Protocol):
    def encrypt(self, plaintext: str) -> str: ...

    def decrypt(self, stored: str) -> str: ...

    def equal(self, left: str, right: str) -> bool: ...


class RateLimiterPort(Protocol):
    def allow(self, key: str, now: float | None = None) -> bool: ...
