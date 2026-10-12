"""Webhook import. Validates a payload and writes one event through apply."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.sync.apply import (
    CAPACITY_MESSAGE,
    MAX_EVENTS,
    RunError,
    RunStats,
    delete_event,
    event_path,
    live_count,
    load_mapping,
    new_uid,
    put_event,
    remember,
    remember_delete,
    utc_now,
)
from app.sync.ical import CalendarEvent, build_vcalendar

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CLOCK = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$")
_RRULE = re.compile(r"^FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(;[A-Z0-9-]+=[A-Z0-9,+-]+)*$")
_STATUSES = {
    "confirmed": "CONFIRMED",
    "tentative": "TENTATIVE",
    "cancelled": "CANCELLED",
}
_UPSERT_KEYS = frozenset(
    {
        "id",
        "action",
        "summary",
        "description",
        "location",
        "start",
        "end",
        "all_day",
        "timezone",
        "rrule",
        "status",
    }
)
_DELETE_KEYS = frozenset({"id", "action"})


class ParseError(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail


@dataclass(frozen=True)
class Upsert:
    external_id: str
    revision: str
    summary: str
    description: str
    location: str
    status: str | None
    start_value: str
    end_value: str | None
    all_day: bool
    timezone: str
    rrule: str | None


@dataclass(frozen=True)
class Delete:
    external_id: str


def parse(payload: object, source_timezone: str) -> Upsert | Delete:
    if not isinstance(payload, dict):
        raise ParseError("The input is invalid.")
    action = payload.get("action")
    if action == "delete":
        return _parse_delete(payload)
    if action == "upsert":
        return _parse_upsert(payload, source_timezone)
    raise ParseError("Action must be upsert or delete.")


def apply(engine: Engine, radicale, source: dict, command: Upsert | Delete) -> RunStats:
    """Write one webhook command. The runner holds the source slot."""

    if isinstance(command, Delete):
        return _apply_delete(engine, radicale, source, command)
    return _apply_upsert(engine, radicale, source, command)


def calendar_bytes(command: Upsert, uid: str) -> bytes:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    text = build_vcalendar(
        CalendarEvent(
            uid=uid,
            summary=command.summary,
            description=command.description,
            location=command.location,
            status=command.status,
            start_value=command.start_value,
            end_value=command.end_value,
            all_day=command.all_day,
            timezone=None if command.all_day else command.timezone,
            rrule=command.rrule,
            dtstamp=stamp,
        )
    )
    return text.encode("utf-8")


def _apply_delete(engine: Engine, radicale, source: dict, command: Delete) -> RunStats:
    with Session(engine) as session:
        mapping = load_mapping(session, source["id"], command.external_id)
        if mapping is None or mapping.deleted_at:
            return RunStats(seen=1)
        uid = mapping.ical_uid
    path = event_path(source["collection_path"], uid)
    try:
        delete_event(radicale, path)
    except RunError as exc:
        raise RunError(exc.message, RunStats(seen=1)) from None
    when = utc_now()
    with Session(engine) as session, session.begin():
        mapping = load_mapping(session, source["id"], command.external_id)
        if mapping is not None:
            remember_delete(mapping, when)
    return RunStats(seen=1, deleted=1)


def _apply_upsert(engine: Engine, radicale, source: dict, command: Upsert) -> RunStats:
    with Session(engine) as session:
        mapping = load_mapping(session, source["id"], command.external_id)
        if (
            mapping is not None
            and mapping.deleted_at is None
            and mapping.external_version == command.revision
        ):
            return RunStats(seen=1)
        is_new = mapping is None or mapping.deleted_at is not None
        uid = mapping.ical_uid if mapping is not None else new_uid()
    if is_new:
        occupied = live_count(radicale, source["collection_path"], RunStats(seen=1))
        if occupied >= MAX_EVENTS:
            raise RunError(CAPACITY_MESSAGE, RunStats(seen=1))
    body = calendar_bytes(command, uid)
    path = event_path(source["collection_path"], uid)
    try:
        put_event(radicale, path, body, fresh=is_new)
    except RunError as exc:
        raise RunError(exc.message, RunStats(seen=1)) from None
    when = utc_now()
    with Session(engine) as session, session.begin():
        remember(session, source["id"], command.external_id, uid, command.revision, when)
    return RunStats(seen=1, upserted=1)


def _parse_delete(payload: dict) -> Delete:
    if set(payload) - _DELETE_KEYS:
        raise ParseError("The input is invalid.")
    return Delete(external_id=_identifier(payload))


def _parse_upsert(payload: dict, source_timezone: str) -> Upsert:
    if set(payload) - _UPSERT_KEYS:
        raise ParseError("The input is invalid.")
    external_id = _identifier(payload)
    summary = _text(payload, "summary", required=True)
    description = _text(payload, "description", required=False)
    location = _text(payload, "location", required=False)
    all_day = _all_day(payload)
    start_text = _required_text(payload, "start", "Enter a start.")
    end_text: str | None
    if all_day:
        start_value = _date_value(start_text, "Enter a start.")
        end_text, end_value = _optional_date(payload)
    else:
        start_value = _clock_value(start_text, "Enter a start.")
        end_text, end_value = _required_clock(payload)
    zone = _zone(payload, source_timezone)
    status = _status(payload)
    rrule = _rrule(payload)
    status_text = None if status is None else str(payload.get("status"))
    canonical = {
        "action": "upsert",
        "all_day": all_day,
        "description": description,
        "end": end_text,
        "id": external_id,
        "location": location,
        "rrule": rrule,
        "start": start_text,
        "status": status_text,
        "summary": summary,
        "timezone": zone,
    }
    return Upsert(
        external_id=external_id,
        revision=_revision(canonical),
        summary=summary,
        description=description,
        location=location,
        status=status,
        start_value=start_value,
        end_value=end_value,
        all_day=all_day,
        timezone=zone,
        rrule=rrule,
    )


def _identifier(payload: dict) -> str:
    value = payload.get("id")
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 256:
        raise ParseError("Enter an id.")
    if any(char in value for char in "\r\n"):
        raise ParseError("Enter an id.")
    return value.strip()


def _text(payload: dict, key: str, *, required: bool) -> str:
    if key not in payload:
        if required:
            raise ParseError("Enter a summary.")
        return ""
    value = payload[key]
    if not isinstance(value, str):
        raise ParseError("The input is invalid.")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _required_text(payload: dict, key: str, message: str) -> str:
    if key not in payload:
        raise ParseError(message)
    value = payload[key]
    if not isinstance(value, str) or not value:
        raise ParseError(message)
    return value


def _all_day(payload: dict) -> bool:
    if "all_day" not in payload:
        return False
    value = payload["all_day"]
    if not isinstance(value, bool):
        raise ParseError("The input is invalid.")
    return value


def _optional_date(payload: dict) -> tuple[str | None, str | None]:
    if "end" not in payload or payload["end"] is None:
        return None, None
    value = payload["end"]
    if not isinstance(value, str):
        raise ParseError("Enter an end.")
    return value, _date_value(value, "Enter an end.")


def _required_clock(payload: dict) -> tuple[str, str]:
    if "end" not in payload or payload["end"] is None:
        raise ParseError("Enter an end.")
    value = payload["end"]
    if not isinstance(value, str):
        raise ParseError("Enter an end.")
    return value, _clock_value(value, "Enter an end.")


def _date_value(value: str, message: str) -> str:
    match = _DATE.fullmatch(value)
    if match is None:
        raise ParseError(message)
    year, month, day = (int(part) for part in match.groups())
    try:
        date(year, month, day)
    except ValueError:
        raise ParseError(message) from None
    return f"{year:04}{month:02}{day:02}"


def _clock_value(value: str, message: str) -> str:
    match = _CLOCK.fullmatch(value)
    if match is None:
        raise ParseError(message)
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
        raise ParseError(message) from None
    compact = f"{year}{month}{day}T{hour}{minute}"
    if second is not None:
        compact += second
    return compact


def _zone(payload: dict, source_timezone: str) -> str:
    if "timezone" not in payload or payload["timezone"] is None:
        zone = source_timezone
    else:
        zone = payload["timezone"]
        if not isinstance(zone, str):
            raise ParseError("Enter a valid time zone.")
        zone = zone.strip()
    if not _timezone_ok(zone):
        raise ParseError("Enter a valid time zone.")
    return zone


def _status(payload: dict) -> str | None:
    if "status" not in payload or payload["status"] is None:
        return None
    value = payload["status"]
    if not isinstance(value, str) or value not in _STATUSES:
        raise ParseError("The input is invalid.")
    return _STATUSES[value]


def _rrule(payload: dict) -> str | None:
    if "rrule" not in payload or payload["rrule"] is None:
        return None
    value = payload["rrule"]
    if not isinstance(value, str) or _RRULE.fullmatch(value) is None:
        raise ParseError("Enter a valid recurrence rule.")
    return value


def _timezone_ok(value: str) -> bool:
    if not value:
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _revision(canonical: dict) -> str:
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
