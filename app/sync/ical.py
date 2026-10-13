"""The only module that builds and reads VEVENT objects."""

import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_CLOCK = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?$")
_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DURATION = re.compile(
    r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
    re.IGNORECASE,
)


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


def build_vcalendar(event: CalendarEvent) -> str:
    return _calendar((event,))


def build_object(obj: CalendarObject) -> str:
    return _calendar(obj.components)


def parse_vcalendar(text: str, source_timezone: str) -> tuple[CalendarObject, ...]:
    """Read one VCALENDAR. A bad document raises before any object is returned."""

    zone = _zone(source_timezone)
    grouped: dict[str, list[CalendarEvent]] = {}
    versions: dict[str, str] = {}
    order: list[str] = []
    for raw in _read_events(text):
        event, is_master, version = _project(raw, zone, source_timezone.strip())
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
    return tuple(objects)


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


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise CalendarParseError() from None


def _read_events(text: str) -> list[list[tuple[str, dict[str, str], str]]]:
    lines = _unfold(text)
    if not lines or lines[0].upper() != "BEGIN:VCALENDAR":
        raise CalendarParseError()
    depth = 0
    vevent_at: int | None = None
    skip_at: int | None = None
    props: list[tuple[str, dict[str, str], str]] = []
    events: list[list[tuple[str, dict[str, str], str]]] = []
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
    if not closed or depth != 0:
        raise CalendarParseError()
    return events


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


def _one(prop: tuple[dict[str, str], str], zone: ZoneInfo, source_name: str) -> Mark:
    marks = _values(prop[0], prop[1], zone, source_name)
    if len(marks) != 1 or marks[0].period_end is not None:
        raise CalendarParseError()
    return marks[0]


def _many(
    found: dict[str, list[tuple[dict[str, str], str]]],
    name: str,
    zone: ZoneInfo,
    source_name: str,
) -> list[Mark]:
    marks: list[Mark] = []
    for params, value in found.get(name, []):
        marks.extend(_values(params, value, zone, source_name))
    return marks


def _values(
    params: dict[str, str], value: str, zone: ZoneInfo, source_name: str
) -> list[Mark]:
    pieces = [piece for piece in value.split(",") if piece.strip()]
    if not pieces:
        raise CalendarParseError()
    period = params.get("VALUE", "").upper() == "PERIOD"
    marks: list[Mark] = []
    for piece in pieces:
        item = piece.strip()
        if period or "/" in item:
            marks.append(_period(item, params, zone, source_name))
        else:
            marks.append(_mark(_instant(item, params), zone, source_name))
    return marks


def _period(
    item: str, params: dict[str, str], zone: ZoneInfo, source_name: str
) -> Mark:
    if "/" not in item:
        raise CalendarParseError()
    left, right = item.split("/", 1)
    start = _instant(left.strip(), params)
    if right.strip().upper().startswith("P"):
        end = _shift(start, right.strip())
    else:
        end = _instant(right.strip(), params)
    start_mark = _mark(start, zone, source_name)
    end_mark = _mark(end, zone, source_name)
    if start_mark.all_day != end_mark.all_day:
        raise CalendarParseError()
    return Mark(
        start_mark.value, start_mark.all_day, start_mark.timezone, end_mark.value
    )


def _end_from_duration(
    start_prop: tuple[dict[str, str], str],
    duration: str,
    zone: ZoneInfo,
    source_name: str,
) -> Mark:
    return _mark(
        _shift(_instant(start_prop[1].strip(), start_prop[0]), duration),
        zone,
        source_name,
    )


def _shift(instant: _Instant, duration: str) -> _Instant:
    amount, date_only, has_seconds = _duration(duration)
    if instant.kind == "date":
        if not date_only:
            raise CalendarParseError()
        if not isinstance(instant.moment, date):
            raise CalendarParseError()
        return _Instant("date", instant.moment + amount, False)
    if not isinstance(instant.moment, datetime):
        raise CalendarParseError()
    return _Instant(
        instant.kind, instant.moment + amount, instant.has_seconds or has_seconds
    )


def _duration(value: str) -> tuple[timedelta, bool, bool]:
    match = _DURATION.fullmatch(value.strip())
    if match is None:
        raise CalendarParseError()
    sign, weeks, days, hours, minutes, seconds = match.groups()
    if not any((weeks, days, hours, minutes, seconds)):
        raise CalendarParseError()
    if "T" in value.upper() and hours is None and minutes is None and seconds is None:
        raise CalendarParseError()
    amount = timedelta(
        weeks=int(weeks or 0),
        days=int(days or 0),
        hours=int(hours or 0),
        minutes=int(minutes or 0),
        seconds=int(seconds or 0),
    )
    if sign == "-":
        amount = -amount
    date_only = hours is None and minutes is None and seconds is None
    return amount, date_only, seconds is not None


def _instant(raw: str, params: dict[str, str]) -> _Instant:
    text = raw.strip()
    if not text:
        raise CalendarParseError()
    kind = params.get("VALUE", "").upper()
    tzid = params.get("TZID")
    if tzid is not None:
        tzid = tzid.strip()
        if not tzid:
            raise CalendarParseError()
    is_utc = text.endswith(("Z", "z"))
    body = text[:-1] if is_utc else text
    if kind == "DATE" or (kind != "DATE-TIME" and _DATE.fullmatch(body) is not None):
        if is_utc:
            raise CalendarParseError()
        return _Instant("date", _date(body), False)
    match = _CLOCK.fullmatch(body)
    if match is None or kind == "DATE":
        raise CalendarParseError()
    year, month, day, hour, minute, second = match.groups()
    has_seconds = second is not None
    try:
        clock = datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second) if second is not None else 0,
        )
    except ValueError:
        raise CalendarParseError() from None
    if is_utc:
        return _Instant("zoned", clock.replace(tzinfo=UTC), has_seconds)
    if tzid:
        return _Instant("zoned", clock.replace(tzinfo=_zone(tzid)), has_seconds)
    return _Instant("floating", clock, has_seconds)


def _mark(instant: _Instant, zone: ZoneInfo, source_name: str) -> Mark:
    if instant.kind == "date":
        if not isinstance(instant.moment, date):
            raise CalendarParseError()
        return Mark(instant.moment.strftime("%Y%m%d"), True, None)
    if not isinstance(instant.moment, datetime):
        raise CalendarParseError()
    if instant.kind == "floating":
        return Mark(
            _format_clock(instant.moment, instant.has_seconds), False, source_name
        )
    local = instant.moment.astimezone(zone)
    return Mark(_format_clock(local, instant.has_seconds), False, source_name)


def _format_clock(moment: datetime, has_seconds: bool) -> str:
    text = moment.strftime("%Y%m%dT%H%M")
    if has_seconds:
        text += f"{moment.second:02d}"
    return text


def _date(value: str) -> date:
    match = _DATE.fullmatch(value)
    if match is None:
        raise CalendarParseError()
    year, month, day = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        raise CalendarParseError() from None


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


def merge_partial(
    existing: CalendarObject,
    master: CalendarEvent | None,
    exceptions: tuple[CalendarEvent, ...],
    exdates: tuple[Mark, ...],
    *,
    uid: str,
    version: str,
) -> CalendarObject:
    """Fold an incremental Google change into the event already stored."""

    masters = [item for item in existing.components if item.recurrence_id is None]
    if master is None and len(masters) != 1:
        raise CalendarParseError()
    base = replace(master if master is not None else masters[0], uid=uid)
    extra = [mark for mark in exdates if not _mark_in(mark, base.exdates)]
    if extra:
        base = replace(base, exdates=base.exdates + tuple(extra))
    kept: list[CalendarEvent] = []
    for item in existing.components:
        if item.recurrence_id is None or _mark_in(item.recurrence_id, base.exdates):
            continue
        kept.append(replace(item, uid=uid))
    for incoming in exceptions:
        recurrence_id = incoming.recurrence_id
        if recurrence_id is None:
            raise CalendarParseError()
        event = replace(incoming, uid=uid)
        if _mark_in(recurrence_id, base.exdates):
            continue
        index = next(
            (
                position
                for position, item in enumerate(kept)
                if item.recurrence_id is not None
                and _mark_in(recurrence_id, (item.recurrence_id,))
            ),
            None,
        )
        if index is None:
            kept.append(event)
        else:
            kept[index] = event
    return CalendarObject(uid=uid, version=version, components=(base, *kept))


def _mark_in(mark: Mark, marks: tuple[Mark, ...]) -> bool:
    return any(
        mark.value == item.value and mark.all_day == item.all_day for item in marks
    )


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


def _unescape(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt in {"n", "N"}:
                output.append("\n")
            elif nxt == "\\":
                output.append("\\")
            else:
                output.append(nxt)
            index += 2
            continue
        output.append(value[index])
        index += 1
    return "".join(output)
