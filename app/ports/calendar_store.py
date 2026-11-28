"""CalendarStore port: CalDAV collection and event operations."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class CalendarStore(Protocol):
    """Collection CRUD/properties; event get/put/delete; REPORT query and multiget."""

    def create_collection(self, path: str, display_name: str, color: str) -> None: ...

    def update_properties(self, path: str, display_name: str, color: str) -> None: ...

    def delete_collection(self, path: str) -> None: ...

    def put_event(self, path: str, body: bytes, *, fresh: bool = False) -> None: ...

    def delete_event(self, path: str) -> None: ...

    def get_event(self, path: str) -> bytes | None: ...

    def list_event_hrefs(self, path: str) -> list[str]: ...

    def count_events(self, path: str) -> int: ...

    def query_events(
        self,
        path: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[tuple[str, bytes]]:
        """CalDAV REPORT calendar-query with optional time-range."""
        ...

    def multiget_events(self, path: str, hrefs: list[str]) -> list[tuple[str, bytes]]:
        """CalDAV calendar-multiget for the given hrefs."""
        ...

    def reachable(self) -> bool: ...
