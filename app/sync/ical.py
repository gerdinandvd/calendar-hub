"""The only module that builds and reads VEVENT objects."""

import re
from dataclasses import replace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.sync.ical_time import (
    _end_from_duration,
    _many,
    _one,
    _unescape,
    _zone,
)
from app.sync.ical_types import (
    CalendarEvent,
    CalendarFeed,
    CalendarObject,
    CalendarParseError,
    Mark,
)

__all__ = [
    "CalendarEvent",
    "CalendarFeed",
    "CalendarObject",
    "CalendarParseError",
    "Mark",
    "build_object",
    "build_vcalendar",
    "parse_feed",
    "parse_vcalendar",
    "with_stored_uid",
]


def build_vcalendar(event: CalendarEvent) -> str:
    return _calendar((event,))


def build_object(obj: CalendarObject) -> str:
    return _calendar(obj.components)


def with_stored_uid(obj: CalendarObject, ical_uid: str) -> CalendarObject:
    """Rewrite component UIDs for Radicale while keeping the external feed UID."""

    return CalendarObject(
        uid=obj.uid,
        version=obj.version,
        components=tuple(replace(item, uid=ical_uid) for item in obj.components),
    )


def parse_vcalendar(text: str, source_timezone: str) -> tuple[CalendarObject, ...]:
    """Read one VCALENDAR. A bad document raises before any object is returned."""

    return parse_feed(text, source_timezone).objects


def parse_feed(text: str, source_timezone: str) -> CalendarFeed:
    """Read one VCALENDAR with optional X-WR calendar fields."""

    events, cal_name, cal_timezone = _read_calendar(text)
    zone_name = source_timezone.strip()
    if cal_timezone and _timezone_name_ok(cal_timezone):
        zone_name = cal_timezone.strip()
    zone = _zone(zone_name)
    grouped: dict[str, list[CalendarEvent]] = {}
    versions: dict[str, str] = {}
    order: list[str] = []
    for raw in events:
        event, is_master, version = _project(raw, zone, zone_name)
        if event.uid not in grouped:
            grouped[event.uid] = []
            order.append(event.uid)
        if is_master:
            if versions.get(event.uid):
                raise CalendarParseError()
            versions[event.uid] = version
            grouped[event.uid].insert(0, event)
        else:
            grouped[event.uid].append(event)
    objects: list[CalendarObject] = []
    for uid in order:
        version = versions.get(uid)
        if not version:
            raise CalendarParseError()
        master_stamp = version
        components = tuple(_with_stamp(item, master_stamp) for item in grouped[uid])
        objects.append(CalendarObject(uid=uid, version=version, components=components))
    name = _clean_cal_text(cal_name)
    zone_out = (
        cal_timezone.strip()
        if cal_timezone and _timezone_name_ok(cal_timezone)
        else None
    )
    return CalendarFeed(objects=tuple(objects), cal_name=name, cal_timezone=zone_out)


def _timezone_name_ok(value: str) -> bool:
    text = value.strip()
    if not text or any(char in text for char in "\r\n"):
        return False
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


def _clean_cal_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = _unescape(value).strip()
    if not text or any(char in text for char in "\r\n"):
        return None
    return text


def _with_stamp(event: CalendarEvent, master_stamp: str) -> CalendarEvent:
    if event.dtstamp:
        return event
    return CalendarEvent(
        uid=event.uid,
        summary=event.summary,
        description=event.description,
        location=event.location,
        status=event.status,
        start_value=event.start_value,
        end_value=event.end_value,
        all_day=event.all_day,
        timezone=event.timezone,
        rrule=event.rrule,
        dtstamp=master_stamp,
        rrules=event.rrules,
        exdates=event.exdates,
        rdates=event.rdates,
        recurrence_id=event.recurrence_id,
    )


def _calendar(events: tuple[CalendarEvent, ...] | tuple[CalendarEvent]) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Calendar Hub//EN",
        "CALSCALE:GREGORIAN",
    ]
    for event in events:
        lines.extend(_event_lines(event))
    lines.append("END:VCALENDAR")
    return "".join(f"{_fold(line)}\r\n" for line in lines)


def _event_lines(event: CalendarEvent) -> list[str]:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{_token(event.uid)}",
        f"DTSTAMP:{_token(event.dtstamp)}",
        _start_line(event),
    ]
    if event.end_value:
        lines.append(_end_line(event))
    if event.recurrence_id is not None:
        lines.append(_mark_line("RECURRENCE-ID", event.recurrence_id))
    lines.append(f"SUMMARY:{_escape(event.summary)}")
    lines.append(f"DESCRIPTION:{_escape(event.description)}")
    lines.append(f"LOCATION:{_escape(event.location)}")
    if event.status:
        lines.append(f"STATUS:{_token(event.status)}")
    for rule in _rules(event):
        lines.append(f"RRULE:{_token(rule)}")
    for mark in event.rdates:
        lines.append(_mark_line("RDATE", mark))
    for mark in event.exdates:
        lines.append(_mark_line("EXDATE", mark))
    lines.append("END:VEVENT")
    return lines


def _rules(event: CalendarEvent) -> list[str]:
    rules: list[str] = []
    if event.rrule:
        rules.append(event.rrule)
    rules.extend(event.rrules)
    return rules


def _start_line(event: CalendarEvent) -> str:
    if event.all_day:
        return f"DTSTART;VALUE=DATE:{_token(event.start_value)}"
    return f"DTSTART;TZID={_token(event.timezone or 'UTC')}:{_token(event.start_value)}"


def _end_line(event: CalendarEvent) -> str:
    if event.all_day:
        return f"DTEND;VALUE=DATE:{_token(event.end_value or '')}"
    return (
        f"DTEND;TZID={_token(event.timezone or 'UTC')}:{_token(event.end_value or '')}"
    )


def _mark_line(name: str, mark: Mark) -> str:
    if mark.period_end is not None:
        if mark.all_day:
            return f"{name};VALUE=PERIOD:{_token(mark.value)}/{_token(mark.period_end)}"
        zone = _token(mark.timezone or "UTC")
        return f"{name};TZID={zone};VALUE=PERIOD:{_token(mark.value)}/{_token(mark.period_end)}"
    if mark.all_day:
        return f"{name};VALUE=DATE:{_token(mark.value)}"
    return f"{name};TZID={_token(mark.timezone or 'UTC')}:{_token(mark.value)}"


def _token(value: str) -> str:
    if any(char in value for char in "\r\n"):
        raise ValueError("calendar token contains a line break")
    return value


def _escape(value: str) -> str:
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n")
    return (
        cleaned.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold(line: str) -> str:
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    pieces: list[bytes] = []
    limit = 75
    start = 0
    while start < len(encoded):
        end = min(start + limit, len(encoded))
        while end > start and end < len(encoded) and 0x80 <= encoded[end] <= 0xBF:
            end -= 1
        if end == start:
            end = min(start + limit, len(encoded))
        pieces.append(encoded[start:end])
        start = end
        limit = 74
    return "\r\n ".join(piece.decode("utf-8") for piece in pieces)


def _read_calendar(
    text: str,
) -> tuple[list[list[tuple[str, dict[str, str], str]]], str | None, str | None]:
    lines = _unfold(text)
    if not lines or lines[0].upper() != "BEGIN:VCALENDAR":
        raise CalendarParseError()
    depth = 0
    vevent_at: int | None = None
    skip_at: int | None = None
    props: list[tuple[str, dict[str, str], str]] = []
    events: list[list[tuple[str, dict[str, str], str]]] = []
    cal_name: str | None = None
    cal_timezone: str | None = None
    closed = False
    for line in lines:
        name, params, value = _content(line)
        if name == "BEGIN":
            depth += 1
            label = value.upper()
            if depth == 1:
                if label != "VCALENDAR" or closed:
                    raise CalendarParseError()
            elif label == "VEVENT" and skip_at is None and vevent_at is None:
                vevent_at = depth
                props = []
            elif vevent_at is None and skip_at is None:
                skip_at = depth
            continue
        if name == "END":
            label = value.upper()
            if vevent_at == depth and label == "VEVENT":
                events.append(props)
                props = []
                vevent_at = None
            if skip_at == depth:
                skip_at = None
            if depth == 1:
                if label != "VCALENDAR":
                    raise CalendarParseError()
                closed = True
            depth -= 1
            if depth < 0:
                raise CalendarParseError()
            continue
        if closed or depth == 0:
            raise CalendarParseError()
        if vevent_at is not None and depth == vevent_at:
            props.append((name, params, value))
        elif depth == 1 and vevent_at is None and skip_at is None:
            if name == "X-WR-CALNAME" and cal_name is None:
                cal_name = value
            elif name == "X-WR-TIMEZONE" and cal_timezone is None:
                cal_timezone = value
    if not closed or depth != 0:
        raise CalendarParseError()
    return events, cal_name, cal_timezone


def _unfold(text: str) -> list[str]:
    cleaned = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"\n[ \t]", "", cleaned)
    return [line for line in cleaned.split("\n") if line != ""]


def _content(line: str) -> tuple[str, dict[str, str], str]:
    index = 0
    size = len(line)
    while index < size and line[index] not in ";:":
        index += 1
    if index == 0 or index >= size:
        raise CalendarParseError()
    name = line[:index].upper()
    params: dict[str, str] = {}
    while index < size and line[index] == ";":
        index += 1
        start = index
        while index < size and line[index] not in "=:;":
            index += 1
        if start == index or index >= size or line[index] != "=":
            raise CalendarParseError()
        key = line[start:index].upper()
        index += 1
        if index < size and line[index] == '"':
            index += 1
            start = index
            while index < size and line[index] != '"':
                index += 1
            if index >= size:
                raise CalendarParseError()
            params[key] = line[start:index]
            index += 1
        else:
            start = index
            while index < size and line[index] not in ";:":
                index += 1
            params[key] = line[start:index]
    if index >= size or line[index] != ":":
        raise CalendarParseError()
    return name, params, line[index + 1 :]


def _project(
    props: list[tuple[str, dict[str, str], str]],
    zone: ZoneInfo,
    source_name: str,
) -> tuple[CalendarEvent, bool, str]:
    found = _index(props)
    uid = _text_value(found, "UID")
    if uid is None or not uid.strip() or any(char in uid for char in "\r\n"):
        raise CalendarParseError()
    uid = uid.strip()
    stamp = _plain(found, "DTSTAMP")
    recurrence = _first(found, "RECURRENCE-ID")
    is_master = recurrence is None
    if is_master and not stamp:
        raise CalendarParseError()
    if stamp and any(char in stamp for char in "\r\n"):
        raise CalendarParseError()
    start_prop = _first(found, "DTSTART")
    if start_prop is None:
        raise CalendarParseError()
    start = _one(start_prop, zone, source_name)
    end_prop = _first(found, "DTEND")
    duration_prop = _first(found, "DURATION")
    end: Mark | None
    if end_prop is not None:
        end = _one(end_prop, zone, source_name)
    elif duration_prop is not None:
        end = _end_from_duration(start_prop, duration_prop[1], zone, source_name)
    else:
        end = None
    if end is not None and end.all_day != start.all_day:
        raise CalendarParseError()
    summary = _text_value(found, "SUMMARY") or ""
    description = _text_value(found, "DESCRIPTION") or ""
    location = _text_value(found, "LOCATION") or ""
    status = _plain(found, "STATUS")
    if status == "":
        status = None
    if status and any(char in status for char in "\r\n"):
        raise CalendarParseError()
    rules = tuple(_plain_all(found, "RRULE"))
    for rule in rules:
        if not rule or any(char in rule for char in "\r\n"):
            raise CalendarParseError()
    event = CalendarEvent(
        uid=uid,
        summary=summary,
        description=description,
        location=location,
        status=status,
        start_value=start.value,
        end_value=None if end is None else end.value,
        all_day=start.all_day,
        timezone=None if start.all_day else source_name,
        rrule=None,
        dtstamp=stamp or "",
        rrules=rules,
        exdates=tuple(_many(found, "EXDATE", zone, source_name)),
        rdates=tuple(_many(found, "RDATE", zone, source_name)),
        recurrence_id=None
        if recurrence is None
        else _one(recurrence, zone, source_name),
    )
    return event, is_master, stamp or ""


def _index(
    props: list[tuple[str, dict[str, str], str]],
) -> dict[str, list[tuple[dict[str, str], str]]]:
    found: dict[str, list[tuple[dict[str, str], str]]] = {}
    for name, params, value in props:
        found.setdefault(name, []).append((params, value))
    return found


def _first(
    found: dict[str, list[tuple[dict[str, str], str]]],
    name: str,
) -> tuple[dict[str, str], str] | None:
    items = found.get(name)
    if not items:
        return None
    return items[0]


def _text_value(
    found: dict[str, list[tuple[dict[str, str], str]]], name: str
) -> str | None:
    item = _first(found, name)
    if item is None:
        return None
    return _unescape(item[1])


def _plain(found: dict[str, list[tuple[dict[str, str], str]]], name: str) -> str:
    item = _first(found, name)
    if item is None:
        return ""
    return item[1].strip()


def _plain_all(
    found: dict[str, list[tuple[dict[str, str], str]]], name: str
) -> list[str]:
    return [value.strip() for _params, value in found.get(name, [])]
