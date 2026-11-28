"""Domain value types. Zero I/O and zero framework imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.domain.errors import CalendarParseError

__all__ = [
    "AgentScope",
    "AgentTokenRecord",
    "CalendarEvent",
    "CalendarFeed",
    "CalendarObject",
    "CalendarParseError",
    "EventMappingRecord",
    "FeedCursor",
    "FeedSnapshot",
    "ImportResult",
    "Mark",
    "NotModified",
    "Occurrence",
    "RunStats",
    "SourceKind",
    "SourceRecord",
    "SyncStateRecord",
    "SyncStatus",
    "TimeWindow",
    "VerifiedToken",
    "_Instant",
]


class SourceKind(StrEnum):
    PRIVATE = "private"
    ICS = "ics"


class SyncStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    NA = "na"


class AgentScope(StrEnum):
    READ = "calendars:read"
    WRITE = "calendars:write"


@dataclass(frozen=True)
class Mark:
    """One date or clock time, already projected onto the source time zone."""

    value: str
    all_day: bool
    timezone: str | None
    period_end: str | None = None


@dataclass(frozen=True)
class CalendarEvent:
    uid: str
    summary: str
    description: str
    location: str
    status: str | None
    start_value: str
    end_value: str | None
    all_day: bool
    timezone: str | None
    dtstamp: str
    rrules: tuple[str, ...] = ()
    exdates: tuple[Mark, ...] = ()
    rdates: tuple[Mark, ...] = ()
    recurrence_id: Mark | None = None


@dataclass(frozen=True)
class CalendarObject:
    """One UID: the master VEVENT plus any RECURRENCE-ID exceptions."""

    uid: str
    version: str
    components: tuple[CalendarEvent, ...]


@dataclass(frozen=True)
class CalendarFeed:
    """Parsed VCALENDAR: events plus optional calendar-level feed fields."""

    objects: tuple[CalendarObject, ...]
    cal_name: str | None = None
    cal_timezone: str | None = None


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Occurrence:
    event: CalendarEvent
    start: str
    end: str | None
    all_day: bool
    timezone: str | None
    is_occurrence: bool


@dataclass(frozen=True)
class SourceRecord:
    id: str
    kind: SourceKind
    name: str
    color: str
    collection_path: str
    enabled: bool
    timezone: str
    created_at: str
    updated_at: str
    poll_seconds: int | None = None
    ics_url: str | None = None
    ics_allow_http: bool = False


@dataclass(frozen=True)
class SyncStateRecord:
    source_id: str
    status: SyncStatus
    cursor: str | None = None
    last_started_at: str | None = None
    last_success_at: str | None = None
    last_error_at: str | None = None
    last_error_message: str | None = None
    events_seen: int = 0
    events_upserted: int = 0
    events_deleted: int = 0


@dataclass(frozen=True)
class EventMappingRecord:
    id: str
    source_id: str
    external_id: str
    ical_uid: str
    external_version: str | None = None
    last_synced_at: str | None = None
    deleted_at: str | None = None


@dataclass(frozen=True)
class AgentTokenRecord:
    id: str
    name: str
    token_hash: str
    token_prefix: str
    scopes: frozenset[str]
    created_at: str
    revoked_at: str | None = None


@dataclass(frozen=True)
class VerifiedToken:
    id: str
    name: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class FeedCursor:
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class NotModified:
    """A conditional GET returned 304. Mappings and events stay as they are."""


@dataclass(frozen=True)
class FeedSnapshot:
    objects: tuple[CalendarObject, ...]
    etag: str | None
    last_modified: str | None
    cal_name: str | None = None
    cal_timezone: str | None = None


@dataclass
class RunStats:
    seen: int = 0
    upserted: int = 0
    deleted: int = 0


@dataclass
class ImportResult:
    stats: RunStats = field(default_factory=RunStats)
    cursor: str | None = None
    save_cursor: bool = False


@dataclass
class _Instant:
    kind: str
    moment: object
    has_seconds: bool
