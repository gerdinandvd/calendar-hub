"""Parse agent-native create/patch payloads into calendar events."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.services.agent.types import SUMMARY_MAX_LEN, TEXT_MAX_LEN, AgentError, invalid
from app.sync.ical import CalendarEvent
from app.sync.time_values import (
    TimeValueError,
    compact_clock,
    compact_date,
    map_rrule,
    map_status,
    timezone_ok,
)

PATCH_KEYS = frozenset(
    {
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
CREATE_KEYS = PATCH_KEYS | {"calendar"}


def parse_create(payload: object) -> dict | AgentError:
    if not isinstance(payload, dict) or set(payload) - CREATE_KEYS:
        return invalid("The input is invalid.")
    core = _create_required(payload)
    if isinstance(core, AgentError):
        return core
    extras = _create_optional(payload)
    if isinstance(extras, AgentError):
        return extras
    return {**core, **extras}


def _create_required(payload: dict) -> dict | AgentError:
    calendar = payload.get("calendar")
    if not isinstance(calendar, str) or not calendar.strip():
        return invalid("Enter a calendar.")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return invalid("Enter a summary.")
    if len(summary) > SUMMARY_MAX_LEN:
        return invalid("The input is invalid.")
    all_day = payload.get("all_day", False)
    if not isinstance(all_day, bool):
        return invalid("The input is invalid.")
    start = payload.get("start")
    end = payload.get("end")
    if not isinstance(start, str) or not isinstance(end, str):
        return invalid("Enter a start and end.")
    try:
        start_value = compact_date(start) if all_day else compact_clock(start)
        end_value = compact_date(end) if all_day else compact_clock(end)
    except TimeValueError as exc:
        return invalid(str(exc))
    if end_value <= start_value:
        return invalid("end must be after start.")
    return {
        "calendar": calendar.strip(),
        "summary": summary,
        "all_day": all_day,
        "start_value": start_value,
        "end_value": end_value,
    }


def _create_optional(payload: dict) -> dict | AgentError:
    timezone = payload.get("timezone")
    if timezone is not None and (
        not isinstance(timezone, str) or not timezone_ok(timezone)
    ):
        return invalid("Enter a valid time zone.")
    description = payload.get("description", "")
    location = payload.get("location", "")
    if description is None:
        description = ""
    if location is None:
        location = ""
    if not isinstance(description, str) or not isinstance(location, str):
        return invalid("The input is invalid.")
    if len(description) > TEXT_MAX_LEN or len(location) > TEXT_MAX_LEN:
        return invalid("The input is invalid.")
    try:
        status = map_status(payload.get("status")) if "status" in payload else None
        rrule = map_rrule(payload.get("rrule")) if "rrule" in payload else None
    except TimeValueError as exc:
        return invalid(str(exc))
    return {
        "description": description.replace("\r\n", "\n").replace("\r", "\n"),
        "location": location.replace("\r\n", "\n").replace("\r", "\n"),
        "timezone": timezone,
        "status": status,
        "rrule": rrule,
    }


def merge_patch(
    master: CalendarEvent, payload: dict, source_timezone: str
) -> CalendarEvent | AgentError:
    if set(payload) - PATCH_KEYS:
        return invalid("The input is invalid.")
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
    error = _apply_patch_fields(data, payload, master, source_timezone)
    if error is not None:
        return error
    if data["end_value"] is not None and data["end_value"] <= data["start_value"]:
        return invalid("end must be after start.")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return replace(
        master,
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
        recurrence_id=None,
    )


def to_calendar_event(parsed: dict, uid: str) -> CalendarEvent:
    zone = parsed["timezone"]
    if parsed["all_day"]:
        zone = None
    elif zone is None or not timezone_ok(zone):
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


def _apply_patch_fields(
    data: dict,
    payload: dict,
    master: CalendarEvent,
    source_timezone: str,
) -> AgentError | None:
    error = _patch_text_fields(data, payload)
    if error is not None:
        return error
    error = _patch_timing(data, payload, master)
    if error is not None:
        return error
    return _patch_meta(data, payload, source_timezone)


def _patch_text_fields(data: dict, payload: dict) -> AgentError | None:
    if "summary" in payload:
        summary = payload["summary"]
        if summary is None or not isinstance(summary, str) or not summary.strip():
            return invalid("Enter a summary.")
        if len(summary) > SUMMARY_MAX_LEN:
            return invalid("The input is invalid.")
        data["summary"] = summary
    for key in ("description", "location"):
        if key not in payload:
            continue
        if payload[key] is None:
            data[key] = ""
        elif isinstance(payload[key], str):
            if len(payload[key]) > TEXT_MAX_LEN:
                return invalid("The input is invalid.")
            data[key] = payload[key].replace("\r\n", "\n").replace("\r", "\n")
        else:
            return invalid("The input is invalid.")
    return None


def _patch_timing(
    data: dict, payload: dict, master: CalendarEvent
) -> AgentError | None:
    if "all_day" in payload:
        if not isinstance(payload["all_day"], bool):
            return invalid("The input is invalid.")
        if payload["all_day"] != master.all_day and (
            "start" not in payload or "end" not in payload
        ):
            return invalid("Enter a start and end.")
        data["all_day"] = payload["all_day"]
    if "start" in payload:
        if not isinstance(payload["start"], str):
            return invalid("Enter a start.")
        try:
            data["start_value"] = (
                compact_date(payload["start"])
                if data["all_day"]
                else compact_clock(payload["start"])
            )
        except TimeValueError as exc:
            return invalid(str(exc))
    if "end" in payload:
        if payload["end"] is None:
            data["end_value"] = None
        elif isinstance(payload["end"], str):
            try:
                data["end_value"] = (
                    compact_date(payload["end"])
                    if data["all_day"]
                    else compact_clock(payload["end"])
                )
            except TimeValueError as exc:
                return invalid(str(exc))
        else:
            return invalid("Enter an end.")
    return None


def _patch_meta(data: dict, payload: dict, source_timezone: str) -> AgentError | None:
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
        try:
            data["status"] = map_status(payload["status"])
        except TimeValueError as exc:
            return invalid(str(exc))
    if "rrule" in payload:
        try:
            data["rrule"] = map_rrule(payload["rrule"])
        except TimeValueError as exc:
            return invalid(str(exc))
    return None
