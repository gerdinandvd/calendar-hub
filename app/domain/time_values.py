"""Shared ISO date/clock, RRULE, status, and timezone helpers."""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.errors import TimeValueError

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CLOCK = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$")
_RRULE = re.compile(r"^FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(;[A-Z0-9-]+=[A-Z0-9,+-]+)*$")
STATUSES = {
    "confirmed": "CONFIRMED",
    "tentative": "TENTATIVE",
    "cancelled": "CANCELLED",
}


def compact_date(value: str, message: str = "Enter a start.") -> str:
    match = _DATE.fullmatch(value)
    if match is None:
        raise TimeValueError(message)
    year, month, day = (int(part) for part in match.groups())
    try:
        date(year, month, day)
    except ValueError as exc:
        raise TimeValueError(message) from exc
    return f"{year:04}{month:02}{day:02}"


def compact_clock(
    value: str, message: str = "Enter a start.", *, pad_seconds: bool = True
) -> str:
    match = _CLOCK.fullmatch(value)
    if match is None:
        raise TimeValueError(message)
    year, month, day, hour, minute, second = match.groups()
    try:
        datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second) if second is not None else 0,
        )
    except ValueError as exc:
        raise TimeValueError(message) from exc
    compact = f"{year}{month}{day}T{hour}{minute}"
    if second is not None:
        compact += second
    elif pad_seconds:
        compact += "00"
    return compact


def map_status(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in STATUSES:
        raise TimeValueError("The input is invalid.")
    return STATUSES[value]


def map_rrule(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _RRULE.fullmatch(value) is None:
        raise TimeValueError("Enter a valid recurrence rule.")
    return value


def timezone_ok(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError, TypeError, KeyError):
        return False
    return True
