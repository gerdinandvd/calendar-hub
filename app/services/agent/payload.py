"""Parse agent-native create/patch payloads into calendar events."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.agent.types import AgentError, invalid
from app.sync.ical import CalendarEvent

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CLOCK = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$")
_RRULE = re.compile(r"^FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(;[A-Z0-9-]+=[A-Z0-9,+-]+)*$")
_STATUSES = {
    "confirmed": "CONFIRMED",
    "tentative": "TENTATIVE",
    "cancelled": "CANCELLED",
}
CREATE_KEYS = frozenset(
    {
        "calendar",
        "summary",
        "start",
        "end",
        "all_day",
        "timezone",
        "description",
        "location",
        "rrule",
        "status",
    }
)
PATCH_KEYS = CREATE_KEYS - {"calendar"}


def parse_create(payload: object) -> dict | AgentError:
    if not isinstance(payload, dict):
        return invalid("The input is invalid.")
    if set(payload) - CREATE_KEYS:
        return invalid("The input is invalid.")
    calendar = payload.get("calendar")
    if not isinstance(calendar, str) or not calendar.strip():
        return invalid("Enter a calendar.")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return invalid("Enter a summary.")
    all_day = payload.get("all_day", False)
    if not isinstance(all_day, bool):
        return invalid("The input is invalid.")
    start = payload.get("start")
    end = payload.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return invalid("Enter a start and end.")
    if all_day:
        start_value = date_value(start)
        end_value = date_value(end)
    else:
        start_value = clock_value(start)
        end_value = clock_value(end)
    if isinstance(start_value, AgentError):
        return start_value
    if isinstance(end_value, AgentError):
        return end_value
    timezone = payload.get("timezone")
    description = payload.get("description", "")
    location = payload.get("location", "")
    if description is None:
        description = ""
    if location is None:
        location = ""
    if not isinstance(description, str) or not isinstance(location, str):
        return invalid("The input is invalid.")
    status = status_value(payload)
    if isinstance(status, AgentError):
        return status
    rrule = rrule_value(payload)
    if isinstance(rrule, AgentError):
        return rrule
    return {
        "calendar": calendar.strip(),
        "summary": summary,
        "description": description.replace("\r\n", "\n").replace("\r", "\n"),
        "location": location.replace("\r\n", "\n").replace("\r", "\n"),
        "all_day": all_day,
        "start_value": start_value,
        "end_value": end_value,
        "timezone": timezone,
        "status": status,
        "rrule": rrule,
    }


def merge_patch(
    master: CalendarEvent, payload: dict, source_timezone: str
) -> CalendarEvent | AgentError:
    data = {
        "summary": master.summary,
        "description": master.description,
        "location": master.location,
        "all_day": master.all_day,
        "start_value": master.start_value,
        "end_value": master.end_value,
        "timezone": master.timezone or source_timezone,
        "status": master.status,
        "rrule": master.rrule,
    }
    if "summary" in payload:
        if payload["summary"] is None or not isinstance(payload["summary"], str):
            return invalid("Enter a summary.")
        data["summary"] = payload["summary"]
    for key in ("description", "location"):
        if key in payload:
            if payload[key] is None:
                data[key] = ""
            elif isinstance(payload[key], str):
                data[key] = payload[key].replace("\r\n", "\n").replace("\r", "\n")
            else:
                return invalid("The input is invalid.")
    if "all_day" in payload:
        if not isinstance(payload["all_day"], bool):
            return invalid("The input is invalid.")
        data["all_day"] = payload["all_day"]
    if "start" in payload:
        if not isinstance(payload["start"], str):
            return invalid("Enter a start.")
        start_value = (
            date_value(payload["start"])
            if data["all_day"]
            else clock_value(payload["start"])
        )
        if isinstance(start_value, AgentError):
            return start_value
        data["start_value"] = start_value
    if "end" in payload:
        if payload["end"] is None:
            data["end_value"] = None
        elif isinstance(payload["end"], str):
            end_value = (
                date_value(payload["end"])
                if data["all_day"]
                else clock_value(payload["end"])
            )
            if isinstance(end_value, AgentError):
                return end_value
            data["end_value"] = end_value
        else:
            return invalid("Enter an end.")
    if "timezone" in payload:
        if payload["timezone"] is None:
            data["timezone"] = source_timezone
        elif isinstance(payload["timezone"], str):
            if not timezone_ok(payload["timezone"]):
                return invalid("Enter a valid time zone.")
            data["timezone"] = payload["timezone"]
        else:
            return invalid("Enter a valid time zone.")
    if "status" in payload:
        status = status_value({"status": payload["status"]})
        if isinstance(status, AgentError):
            return status
        data["status"] = status
    if "rrule" in payload:
        rrule = rrule_value({"rrule": payload["rrule"]})
        if isinstance(rrule, AgentError):
            return rrule
        data["rrule"] = rrule
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return CalendarEvent(
        uid=master.uid,
        summary=data["summary"],
        description=data["description"],
        location=data["location"],
        status=data["status"],
        start_value=data["start_value"],
        end_value=data["end_value"],
        all_day=data["all_day"],
        timezone=None if data["all_day"] else data["timezone"],
        rrule=data["rrule"],
        dtstamp=stamp,
        rrules=(),
        exdates=(),
        rdates=(),
        recurrence_id=None,
    )


def to_calendar_event(parsed: dict, uid: str) -> CalendarEvent:
    zone = parsed["timezone"]
    if parsed["all_day"]:
        zone = None
    elif zone is None:
        zone = "UTC"
    elif not timezone_ok(zone):
        zone = "UTC"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return CalendarEvent(
        uid=uid,
        summary=parsed["summary"],
        description=parsed["description"],
        location=parsed["location"],
        status=parsed["status"],
        start_value=parsed["start_value"],
        end_value=parsed["end_value"],
        all_day=parsed["all_day"],
        timezone=zone,
        rrule=parsed["rrule"],
        dtstamp=stamp,
    )


def date_value(value: str) -> str | AgentError:
    match = _DATE.fullmatch(value)
    if match is None:
        return invalid("Enter a start.")
    year, month, day = (int(part) for part in match.groups())
    try:
        date(year, month, day)
    except ValueError:
        return invalid("Enter a start.")
    return f"{year:04}{month:02}{day:02}"


def clock_value(value: str) -> str | AgentError:
    match = _CLOCK.fullmatch(value)
    if match is None:
        return invalid("Enter a start.")
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
    except ValueError:
        return invalid("Enter a start.")
    compact = f"{year}{month}{day}T{hour}{minute}"
    if second is not None:
        compact += second
    else:
        compact += "00"
    return compact


def status_value(payload: dict) -> str | None | AgentError:
    if "status" not in payload or payload["status"] is None:
        return None
    value = payload["status"]
    if not isinstance(value, str) or value not in _STATUSES:
        return invalid("The input is invalid.")
    return _STATUSES[value]


def rrule_value(payload: dict) -> str | None | AgentError:
    if "rrule" not in payload or payload["rrule"] is None:
        return None
    value = payload["rrule"]
    if not isinstance(value, str) or _RRULE.fullmatch(value) is None:
        return invalid("Enter a valid recurrence rule.")
    return value


def timezone_ok(value: str) -> bool:
    if not value:
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True
