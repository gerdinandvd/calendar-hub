"""SyncCoordinator port: per-source lock, inflight, and stop."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

from app.domain.types import RunStats


class SyncCoordinator(Protocol):
    """Per-source exclusion used by ICS sync and MCP writes that touch a calendar."""

    def acquire(
        self, source_id: str, *, wait: bool = True
    ) -> AbstractContextManager[None]:
        """Hold the source slot for the duration of the context."""
        ...

    def start_background(
        self,
        source_id: str,
        *,
        actor: str,
        work: Callable[[], RunStats],
    ) -> None:
        """Mark running and continue work on a background thread."""
        ...

    def run(
        self,
        source_id: str,
        *,
        actor: str,
        work: Callable[[], RunStats],
        wait: bool = True,
    ) -> RunStats:
        """Run work under the source slot and return stats."""
        ...

    def is_stopping(self) -> bool: ...

    def arm(self) -> None:
        """Allow runs again after process start."""
        ...

    def stop_all(self, timeout: float | None = None) -> None:
        """Signal stop and wait for background runs."""
        ...

    def wait_for(self, source_id: str, timeout: float = 5) -> str:
        """Block until the source is no longer running; return status."""
        ...
