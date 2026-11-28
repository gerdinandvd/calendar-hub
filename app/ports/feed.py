"""FeedFetcher port: bounded HTTP GET for ICS feeds."""

from __future__ import annotations

from typing import Protocol

from app.domain.types import FeedCursor, FeedSnapshot, NotModified


class FeedFetcher(Protocol):
    def fetch(
        self,
        url: str,
        *,
        allow_http: bool,
        authorization: str | None,
        cursor: FeedCursor | None,
        source_timezone: str,
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> FeedSnapshot | NotModified: ...
