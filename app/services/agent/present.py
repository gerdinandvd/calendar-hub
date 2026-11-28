"""Serialize agent event payloads at compact/standard field levels."""

from __future__ import annotations

from app.services.agent.expand import Occurrence, _agent_instant
from app.services.agent.types import DESCRIPTION_COMPACT_LEN, FIELD_COMPACT
from app.sync.ical_types import CalendarEvent


def present_occurrence(
    *,
    hub_id: str,
    calendar_id: str,
    item: Occurrence,
    fields: str,
) -> dict:
    body = {
        "id": hub_id,
        "calendar": calendar_id,
        "summary": item.event.summary,
        "start": item.start,
        "end": item.end,
        "all_day": item.all_day,
        "status": _status_out(item.event.status),
    }
    if item.is_occurrence:
        body["occurrence"] = True
        body["occurrence_start"] = item.start
    return _enrich(body, item.event, fields, item.timezone)


def present_master(
    *,
    hub_id: str,
    calendar_id: str,
    event: CalendarEvent,
    fields: str,
) -> dict:
    body = {
        "id": hub_id,
        "calendar": calendar_id,
        "summary": event.summary,
        "start": _agent_instant(event.start_value, event.all_day),
        "end": (
            None
            if event.end_value is None
            else _agent_instant(event.end_value, event.all_day)
        ),
        "all_day": event.all_day,
        "status": _status_out(event.status),
    }
    return _enrich(body, event, fields, None if event.all_day else event.timezone)


def _enrich(
    body: dict, event: CalendarEvent, fields: str, timezone: str | None
) -> dict:
    if fields == FIELD_COMPACT:
        return body
    description = event.description or ""
    if len(description) > DESCRIPTION_COMPACT_LEN:
        description = description[: DESCRIPTION_COMPACT_LEN - 1] + "…"
    body["description"] = description
    body["location"] = event.location or ""
    body["timezone"] = timezone
    body["rrule"] = event.rrules[0] if event.rrules else None
    return body


def _status_out(value: str | None) -> str | None:
    if value is None:
        return None
    mapping = {
        "CONFIRMED": "confirmed",
        "TENTATIVE": "tentative",
        "CANCELLED": "cancelled",
    }
    return mapping.get(value, value.lower())
