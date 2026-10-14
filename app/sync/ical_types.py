"""Shared calendar domain types for VEVENT build and parse."""

from dataclasses import dataclass
from datetime import date, datetime


class CalendarParseError(Exception):
    """The whole calendar document was rejected. Nothing was written."""

    def __init__(self) -> None:
        self.detail = "The calendar feed could not be read."


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
    rrule: str | None
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


@dataclass
class _Instant:
    kind: str
    moment: date | datetime
    has_seconds: bool
