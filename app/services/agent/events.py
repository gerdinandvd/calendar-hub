"""Agent event read/write using hub opaque ids."""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import EventMapping, Source
from app.radicale.client import RadicaleClient, RadicaleError
from app.services.agent.calendars import load_enabled_sources
from app.services.agent.confirm import ConfirmStore
from app.services.agent.expand import expand_object, master_for_response
from app.services.agent.present import present_master, present_occurrence
from app.services.agent.types import (
    DEFAULT_LIMIT,
    DEFAULT_LIST_DAYS,
    FIELD_COMPACT,
    MAX_LIMIT,
    MAX_WINDOW_DAYS,
    VALID_FIELDS,
    WRITABLE_KINDS,
    AgentError,
    internal,
    invalid,
    not_found,
)
from app.sync.apply import (
    MAX_EVENTS,
    RunError,
    delete_event,
    event_path,
    live_count,
    load_mapping_by_id,
    load_mapping_by_uid,
    new_uid,
    put_event,
    remember,
    remember_delete,
    utc_now,
)
from app.sync.ical import CalendarEvent, build_vcalendar, parse_vcalendar
from app.sync.ical_types import CalendarParseError

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CLOCK = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$")
_RRULE = re.compile(r"^FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(;[A-Z0-9-]+=[A-Z0-9,+-]+)*$")
_STATUSES = {
    "confirmed": "CONFIRMED",
    "tentative": "TENTATIVE",
    "cancelled": "CANCELLED",
}
_CREATE_KEYS = frozenset(
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
_PATCH_KEYS = _CREATE_KEYS - {"calendar"}


def list_or_search_events(
    engine: Engine,
    radicale: RadicaleClient,
    *,
    mode: str,
    q: str | None,
    window_from: str | None,
    window_to: str | None,
    calendars: list[str] | None,
    limit: int | None,
    cursor: str | None,
    fields: str | None,
) -> dict | AgentError:
    field_level = fields or FIELD_COMPACT
    if field_level not in VALID_FIELDS:
        return invalid("fields must be compact, standard, or full.")
    capped = DEFAULT_LIMIT if limit is None else limit
    if not isinstance(capped, int) or capped < 1 or capped > MAX_LIMIT:
        return invalid(f"limit must be between 1 and {MAX_LIMIT}.")

    applied: dict[str, Any] = {}
    query = (q or "").strip()
    if mode == "search":
        if not query:
            return invalid("q is required for search.")
        window, window_error = _resolve_search_window(window_from, window_to)
        if window_error is not None:
            return window_error
        start, end = window if window is not None else (None, None)
    else:
        window, window_error, applied = _resolve_list_window(window_from, window_to)
        if window_error is not None:
            return window_error
        assert window is not None
        start, end = window

    sources = load_enabled_sources(engine, calendars)
    if calendars:
        found_ids = {item["id"] for item in sources}
        missing = [item for item in calendars if item not in found_ids]
        if missing:
            return invalid("Unknown or disabled calendar filter.")

    try:
        rows = _collect(
            engine,
            radicale,
            sources,
            start=start,
            end=end,
            query=query or None,
            fields=field_level,
        )
    except RadicaleError:
        return internal("The calendar could not be read.")
    except CalendarParseError:
        return internal("The calendar could not be read.")

    rows.sort(
        key=lambda item: (
            item["start"] or "",
            item["id"],
            item.get("occurrence_start") or "",
        )
    )
    offset = _decode_cursor(cursor)
    if cursor is not None and offset is None:
        return invalid("cursor is invalid.")
    start_at = 0 if offset is None else offset
    slice_rows = rows[start_at : start_at + capped]
    next_cursor = None
    if start_at + capped < len(rows):
        next_cursor = _encode_cursor(start_at + capped)

    echo_window = None
    if start is not None and end is not None:
        echo_window = {"from": _iso_out(start), "to": _iso_out(end)}

    return {
        "events": slice_rows,
        "window": echo_window,
        "q": query or None,
        "truncated": field_level,
        "applied_defaults": applied,
        "pagination": {
            "limit": capped,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "count": len(slice_rows),
            "truncated": len(rows) > start_at + capped,
        },
    }


def get_event(
    engine: Engine,
    radicale: RadicaleClient,
    event_id: str,
    *,
    fields: str | None = None,
) -> dict | AgentError:
    field_level = fields or FIELD_COMPACT
    if field_level not in VALID_FIELDS:
        return invalid("fields must be compact, standard, or full.")
    loaded = _load_live(engine, radicale, event_id)
    if isinstance(loaded, AgentError):
        return loaded
    mapping, source, obj = loaded
    master = master_for_response(obj)
    return present_master(
        hub_id=mapping.id,
        calendar_id=source["id"],
        event=master,
        fields=field_level,
    )


def create_event(
    engine: Engine, radicale: RadicaleClient, payload: object
) -> dict | AgentError:
    from app.sync.apply import RunStats

    parsed = _parse_create(payload)
    if isinstance(parsed, AgentError):
        return parsed
    source = _writable_source(engine, parsed["calendar"])
    if isinstance(source, AgentError):
        return source
    if parsed["timezone"] is None and not parsed["all_day"]:
        parsed["timezone"] = source["timezone"]
    elif parsed["timezone"] is not None and not _timezone_ok(parsed["timezone"]):
        return invalid("Enter a valid time zone.")
    mapping_id = str(uuid.uuid4())
    ical_uid = new_uid()
    event = _to_calendar_event(parsed, ical_uid)
    body = build_vcalendar(event).encode("utf-8")
    path = event_path(source["collection_path"], ical_uid)
    try:
        occupied = live_count(radicale, source["collection_path"], RunStats())
    except RunError as exc:
        return internal(exc.message)
    if occupied >= MAX_EVENTS:
        return invalid("This calendar already has 20000 events.")
    try:
        put_event(radicale, path, body, fresh=True)
    except RunError as exc:
        return internal(exc.message)
    when = utc_now()
    with Session(engine) as session, session.begin():
        remember(
            session,
            source["id"],
            f"agent:{mapping_id}",
            ical_uid,
            when,
            when,
            mapping_id=mapping_id,
        )
    return get_event(engine, radicale, mapping_id, fields=FIELD_COMPACT)


def update_event(
    engine: Engine, radicale: RadicaleClient, event_id: str, payload: object
) -> dict | AgentError:
    if not isinstance(payload, dict):
        return invalid("The input is invalid.")
    if set(payload) - _PATCH_KEYS:
        return invalid("The input is invalid.")
    loaded = _load_live(engine, radicale, event_id)
    if isinstance(loaded, AgentError):
        return loaded
    mapping, source, obj = loaded
    if source["kind"] not in WRITABLE_KINDS:
        return invalid("This calendar is read-only.")
    master = master_for_response(obj)
    merged = _merge_patch(master, payload, source["timezone"])
    if isinstance(merged, AgentError):
        return merged
    body = build_vcalendar(merged).encode("utf-8")
    path = event_path(source["collection_path"], mapping.ical_uid)
    try:
        put_event(radicale, path, body, fresh=False)
    except RunError as exc:
        return internal(exc.message)
    when = utc_now()
    with Session(engine) as session, session.begin():
        row = load_mapping_by_id(session, mapping.id)
        if row is not None:
            row.external_version = when
            row.last_synced_at = when
            row.deleted_at = None
    return get_event(engine, radicale, mapping.id, fields=FIELD_COMPACT)


def delete_event_flow(
    engine: Engine,
    radicale: RadicaleClient,
    confirm_store: ConfirmStore,
    event_id: str,
    confirm_token: str | None,
) -> dict | AgentError:
    loaded = _load_live(engine, radicale, event_id)
    if isinstance(loaded, AgentError):
        return loaded
    mapping, source, obj = loaded
    if source["kind"] not in WRITABLE_KINDS:
        return invalid("This calendar is read-only.")
    preview = present_master(
        hub_id=mapping.id,
        calendar_id=source["id"],
        event=master_for_response(obj),
        fields=FIELD_COMPACT,
    )
    if not confirm_token:
        token, expires_at = confirm_store.issue(mapping.id, preview)
        return {
            "preview": preview,
            "confirm_token": token,
            "expires_at": datetime.fromtimestamp(expires_at, UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        }
    record = confirm_store.consume(confirm_token, mapping.id)
    if record is None:
        return invalid("confirm_token is invalid or expired.")
    path = event_path(source["collection_path"], mapping.ical_uid)
    try:
        delete_event(radicale, path)
    except RunError as exc:
        return internal(exc.message)
    when = utc_now()
    with Session(engine) as session, session.begin():
        row = load_mapping_by_id(session, mapping.id)
        if row is not None:
            remember_delete(row, when)
    return {"ok": True, "id": mapping.id}


def ensure_mapping_for_uid(
    engine: Engine, source_id: str, ical_uid: str
) -> EventMapping:
    with Session(engine) as session, session.begin():
        existing = load_mapping_by_uid(session, source_id, ical_uid)
        if existing is not None:
            session.expunge(existing)
            return existing
        mapping_id = str(uuid.uuid4())
        when = utc_now()
        row = EventMapping(
            id=mapping_id,
            source_id=source_id,
            external_id=f"caldav:{ical_uid}",
            ical_uid=ical_uid,
            external_version=when,
            last_synced_at=when,
            deleted_at=None,
        )
        session.add(row)
        session.flush()
        session.expunge(row)
        return row


def _collect(
    engine: Engine,
    radicale: RadicaleClient,
    sources: list[dict],
    *,
    start: datetime | None,
    end: datetime | None,
    query: str | None,
    fields: str,
) -> list[dict]:
    needle = query.lower() if query else None
    # Search without window still needs a scan bound for expansion.
    if start is None or end is None:
        start = datetime.now(UTC) - timedelta(days=MAX_WINDOW_DAYS)
        end = datetime.now(UTC) + timedelta(days=MAX_WINDOW_DAYS)
    found: list[dict] = []
    for source in sources:
        hrefs = radicale.list_event_hrefs(source["collection_path"])
        for href in hrefs:
            payload = radicale.get_event(href)
            if payload is None:
                continue
            ical_uid = _uid_from_href(href)
            try:
                objects = parse_vcalendar(payload.decode("utf-8"), source["timezone"])
            except (UnicodeDecodeError, CalendarParseError):
                continue
            mapping = ensure_mapping_for_uid(engine, source["id"], ical_uid)
            for obj in objects:
                # Prefer UID from object when present.
                if obj.uid and obj.uid != ical_uid:
                    mapping = ensure_mapping_for_uid(engine, source["id"], obj.uid)
                for item in expand_object(obj, start, end):
                    if needle and not _matches(item.event, needle):
                        continue
                    found.append(
                        present_occurrence(
                            hub_id=mapping.id,
                            calendar_id=source["id"],
                            item=item,
                            fields=fields,
                        )
                    )
    return found


def _load_live(
    engine: Engine, radicale: RadicaleClient, event_id: str
) -> tuple[EventMapping, dict, Any] | AgentError:
    with Session(engine) as session:
        mapping = load_mapping_by_id(session, event_id)
        if mapping is None or mapping.deleted_at is not None:
            return not_found()
        source_row = session.get(Source, mapping.source_id)
        if source_row is None or source_row.enabled != 1:
            return not_found()
        source = {
            "id": source_row.id,
            "kind": source_row.kind,
            "timezone": source_row.timezone,
            "collection_path": source_row.collection_path,
        }
        ical_uid = mapping.ical_uid
        mapping_copy = EventMapping(
            id=mapping.id,
            source_id=mapping.source_id,
            external_id=mapping.external_id,
            ical_uid=mapping.ical_uid,
            external_version=mapping.external_version,
            last_synced_at=mapping.last_synced_at,
            deleted_at=mapping.deleted_at,
        )
    path = event_path(source["collection_path"], ical_uid)
    try:
        payload = radicale.get_event(path)
    except RadicaleError:
        return internal("The calendar could not be read.")
    if payload is None:
        return not_found()
    try:
        objects = parse_vcalendar(payload.decode("utf-8"), source["timezone"])
    except (UnicodeDecodeError, CalendarParseError):
        return internal("The calendar could not be read.")
    if not objects:
        return not_found()
    return mapping_copy, source, objects[0]


def _writable_source(engine: Engine, calendar_id: str) -> dict | AgentError:
    with Session(engine) as session:
        row = session.get(Source, calendar_id)
        if row is None or row.enabled != 1:
            return not_found("Calendar not found.")
        if row.kind not in WRITABLE_KINDS:
            return invalid("This calendar is read-only.")
        return {
            "id": row.id,
            "kind": row.kind,
            "timezone": row.timezone,
            "collection_path": row.collection_path,
        }


def _parse_create(payload: object) -> dict | AgentError:
    if not isinstance(payload, dict):
        return invalid("The input is invalid.")
    if set(payload) - _CREATE_KEYS:
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
        start_value = _date_value(start)
        end_value = _date_value(end)
    else:
        start_value = _clock_value(start)
        end_value = _clock_value(end)
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
    status = _status(payload)
    if isinstance(status, AgentError):
        return status
    rrule = _rrule(payload)
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


def _merge_patch(
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
            _date_value(payload["start"])
            if data["all_day"]
            else _clock_value(payload["start"])
        )
        if isinstance(start_value, AgentError):
            return start_value
        data["start_value"] = start_value
    if "end" in payload:
        if payload["end"] is None:
            data["end_value"] = None
        elif isinstance(payload["end"], str):
            end_value = (
                _date_value(payload["end"])
                if data["all_day"]
                else _clock_value(payload["end"])
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
            if not _timezone_ok(payload["timezone"]):
                return invalid("Enter a valid time zone.")
            data["timezone"] = payload["timezone"]
        else:
            return invalid("Enter a valid time zone.")
    if "status" in payload:
        status = _status({"status": payload["status"]})
        if isinstance(status, AgentError):
            return status
        data["status"] = status
    if "rrule" in payload:
        rrule = _rrule({"rrule": payload["rrule"]})
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


def _to_calendar_event(parsed: dict, uid: str) -> CalendarEvent:
    zone = parsed["timezone"]
    if parsed["all_day"]:
        zone = None
    elif zone is None:
        zone = "UTC"
    elif not _timezone_ok(zone):
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


def _resolve_list_window(
    window_from: str | None, window_to: str | None
) -> tuple[tuple[datetime, datetime] | None, AgentError | None, dict]:
    applied: dict[str, Any] = {}
    if window_from is None and window_to is None:
        start = datetime.now(UTC)
        end = start + timedelta(days=DEFAULT_LIST_DAYS)
        applied["window"] = "now/+7d"
        return (start, end), None, applied
    return (*_parse_window(window_from, window_to), {})


def _resolve_search_window(
    window_from: str | None, window_to: str | None
) -> tuple[tuple[datetime, datetime] | None, AgentError | None]:
    if window_from is None and window_to is None:
        return None, None
    window, error = _parse_window(window_from, window_to)
    return window, error


def _parse_window(
    window_from: str | None, window_to: str | None
) -> tuple[tuple[datetime, datetime] | None, AgentError | None]:
    if window_from is None or window_to is None:
        return None, invalid("from and to are required together.")
    start = _parse_bound(window_from)
    end = _parse_bound(window_to)
    if isinstance(start, AgentError):
        return None, start
    if isinstance(end, AgentError):
        return None, end
    if end <= start:
        return None, invalid("to must be after from.")
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        return None, invalid(f"window must be at most {MAX_WINDOW_DAYS} days.")
    return (start, end), None


def _parse_bound(value: str) -> datetime | AgentError:
    if _DATE.fullmatch(value):
        year, month, day = (int(part) for part in value.split("-"))
        try:
            return datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            return invalid("Enter a valid from/to.")
    match = _CLOCK.fullmatch(value)
    if match is None:
        return invalid("Enter a valid from/to.")
    year, month, day, hour, minute, second = match.groups()
    try:
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second) if second is not None else 0,
            tzinfo=UTC,
        )
    except ValueError:
        return invalid("Enter a valid from/to.")


def _date_value(value: str) -> str | AgentError:
    match = _DATE.fullmatch(value)
    if match is None:
        return invalid("Enter a start.")
    year, month, day = (int(part) for part in match.groups())
    try:
        date(year, month, day)
    except ValueError:
        return invalid("Enter a start.")
    return f"{year:04}{month:02}{day:02}"


def _clock_value(value: str) -> str | AgentError:
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


def _status(payload: dict) -> str | None | AgentError:
    if "status" not in payload or payload["status"] is None:
        return None
    value = payload["status"]
    if not isinstance(value, str) or value not in _STATUSES:
        return invalid("The input is invalid.")
    return _STATUSES[value]


def _rrule(payload: dict) -> str | None | AgentError:
    if "rrule" not in payload or payload["rrule"] is None:
        return None
    value = payload["rrule"]
    if not isinstance(value, str) or _RRULE.fullmatch(value) is None:
        return invalid("Enter a valid recurrence rule.")
    return value


def _timezone_ok(value: str) -> bool:
    if not value:
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _matches(event: CalendarEvent, needle: str) -> bool:
    hay = f"{event.summary}\n{event.description}\n{event.location}".lower()
    return needle in hay


def _uid_from_href(href: str) -> str:
    name = unquote(href.rstrip("/").rsplit("/", 1)[-1])
    if name.lower().endswith(".ics"):
        name = name[:-4]
    return name


def _encode_cursor(offset: int) -> str:
    raw = json.dumps({"o": offset}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str | None) -> int | None:
    if cursor is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        offset = data["o"]
        if not isinstance(offset, int) or offset < 0:
            return None
        return offset
    except (KeyError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _iso_out(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
