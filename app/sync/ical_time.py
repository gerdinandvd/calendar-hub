"""Instant, mark, and duration helpers for VEVENT parse."""

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.sync.ical_types import CalendarParseError, Mark, _Instant

_CLOCK = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?$")
_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_DURATION = re.compile(
    r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$",
    re.IGNORECASE,
)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise CalendarParseError() from None


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
