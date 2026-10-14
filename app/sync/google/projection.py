"""Project Google Calendar API shapes onto shared calendar Marks and events."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.sync.ical import (
    _content,
    _date,
    _format_clock,
    _values,
    _zone,
)
from app.sync.ical_types import CalendarParseError, Mark

_GOOGLE_CLOCK = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})?$"
)


@dataclass(frozen=True)
class RecurrenceParts:
    rules: tuple[str, ...]
    rdates: tuple[Mark, ...]
    exdates: tuple[Mark, ...]


def google_point(value: object, source_timezone: str) -> Mark:
    """Project one Google date or dateTime onto the source time zone."""

    if not isinstance(value, dict):
        raise CalendarParseError()
    name = source_timezone.strip()
    zone = _zone(name)
    if "date" in value and "dateTime" not in value:
        raw = value.get("date")
        if not isinstance(raw, str):
            raise CalendarParseError()
        return Mark(_date(raw.replace("-", "")).strftime("%Y%m%d"), True, None)
    raw = value.get("dateTime")
    if not isinstance(raw, str):
        raise CalendarParseError()
    return _google_datetime(raw, value.get("timeZone"), zone, name)


def recurrence_parts(lines: object, source_timezone: str) -> RecurrenceParts:
    """Split Google recurrence lines into rules, RDATE marks, and EXDATE marks."""

    if lines is None:
        return RecurrenceParts((), (), ())
    if not isinstance(lines, list):
        raise CalendarParseError()
    name = source_timezone.strip()
    zone = _zone(name)
    rules: list[str] = []
    rdates: list[Mark] = []
    exdates: list[Mark] = []
    for line in lines:
        if not isinstance(line, str) or not line.strip():
            raise CalendarParseError()
        prop, params, text = _content(line.strip())
        if prop == "RRULE":
            rule = text.strip()
            if not rule or any(char in rule for char in "\r\n"):
                raise CalendarParseError()
            rules.append(rule)
        elif prop == "RDATE":
            rdates.extend(_values(params, text, zone, name))
        elif prop == "EXDATE":
            exdates.extend(_values(params, text, zone, name))
        else:
            raise CalendarParseError()
    return RecurrenceParts(tuple(rules), tuple(rdates), tuple(exdates))


def _google_datetime(
    raw: str, time_zone: object, zone: ZoneInfo, source_name: str
) -> Mark:
    match = _GOOGLE_CLOCK.fullmatch(raw.strip())
    if match is None:
        raise CalendarParseError()
    year, month, day, hour, minute, second, offset = match.groups()
    has_seconds = second is not None
    try:
        clock = datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second or 0),
        )
    except ValueError:
        raise CalendarParseError() from None
    if offset is None:
        local_name = source_name
        if isinstance(time_zone, str) and time_zone.strip():
            local_name = time_zone.strip()
        aware = clock.replace(tzinfo=_zone(local_name))
    elif offset == "Z":
        aware = clock.replace(tzinfo=UTC)
    else:
        sign = 1 if offset[0] == "+" else -1
        aware = clock.replace(
            tzinfo=timezone(
                sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))
            )
        )
    local = aware.astimezone(zone)
    return Mark(_format_clock(local, has_seconds), False, source_name)
