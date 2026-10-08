"""The only builder of VEVENT objects."""

from dataclasses import dataclass


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


def build_vcalendar(event: CalendarEvent) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Calendar Hub//EN",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{_token(event.uid)}",
        f"DTSTAMP:{_token(event.dtstamp)}",
        _start_line(event),
    ]
    if event.end_value:
        lines.append(_end_line(event))
    lines.append(f"SUMMARY:{_escape(event.summary)}")
    lines.append(f"DESCRIPTION:{_escape(event.description)}")
    lines.append(f"LOCATION:{_escape(event.location)}")
    if event.status:
        lines.append(f"STATUS:{_token(event.status)}")
    if event.rrule:
        lines.append(f"RRULE:{_token(event.rrule)}")
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return "".join(f"{_fold(line)}\r\n" for line in lines)


def _start_line(event: CalendarEvent) -> str:
    if event.all_day:
        return f"DTSTART;VALUE=DATE:{_token(event.start_value)}"
    return f"DTSTART;TZID={_token(event.timezone or 'UTC')}:{_token(event.start_value)}"


def _end_line(event: CalendarEvent) -> str:
    if event.all_day:
        return f"DTEND;VALUE=DATE:{_token(event.end_value or '')}"
    return f"DTEND;TZID={_token(event.timezone or 'UTC')}:{_token(event.end_value or '')}"


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
