"""Agent event read/write using hub opaque ids."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import EventMapping, Source
from app.radicale.client import RadicaleClient, RadicaleError
from app.services.agent.calendars import load_enabled_sources
from app.services.agent.confirm import ConfirmStore
from app.services.agent.expand import master_for_response
from app.services.agent.payload import (
    PATCH_KEYS,
    merge_patch,
    parse_create,
    timezone_ok,
    to_calendar_event,
)
from app.services.agent.present import present_master
from app.services.agent.query import (
    collect_events,
    decode_cursor,
    encode_cursor,
    iso_out,
    resolve_list_window,
    resolve_search_window,
)
from app.services.agent.types import (
    DEFAULT_LIMIT,
    FIELD_COMPACT,
    MAX_LIMIT,
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
    RunStats,
    delete_event,
    event_path,
    live_count,
    load_mapping_by_id,
    new_uid,
    put_event,
    remember,
    remember_delete,
    utc_now,
)
from app.sync.ical import build_vcalendar, parse_vcalendar
from app.sync.ical_types import CalendarParseError


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
        window, window_error = resolve_search_window(window_from, window_to)
        if window_error is not None:
            return window_error
        start, end = window if window is not None else (None, None)
    else:
        window, window_error, applied = resolve_list_window(window_from, window_to)
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
        rows = collect_events(
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
    offset = decode_cursor(cursor)
    if cursor is not None and offset is None:
        return invalid("cursor is invalid.")
    start_at = 0 if offset is None else offset
    slice_rows = rows[start_at : start_at + capped]
    next_cursor = None
    if start_at + capped < len(rows):
        next_cursor = encode_cursor(start_at + capped)

    echo_window = None
    if start is not None and end is not None:
        echo_window = {"from": iso_out(start), "to": iso_out(end)}

    return {
        "events": slice_rows,
        "window": echo_window,
        "q": query or None,
        "fields": field_level,
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
    parsed = parse_create(payload)
    if isinstance(parsed, AgentError):
        return parsed
    source = _writable_source(engine, parsed["calendar"])
    if isinstance(source, AgentError):
        return source
    if parsed["timezone"] is None and not parsed["all_day"]:
        parsed["timezone"] = source["timezone"]
    elif parsed["timezone"] is not None and not timezone_ok(parsed["timezone"]):
        return invalid("Enter a valid time zone.")
    mapping_id = str(uuid.uuid4())
    ical_uid = new_uid()
    event = to_calendar_event(parsed, ical_uid)
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
    if set(payload) - PATCH_KEYS:
        return invalid("The input is invalid.")
    loaded = _load_live(engine, radicale, event_id)
    if isinstance(loaded, AgentError):
        return loaded
    mapping, source, obj = loaded
    if source["kind"] not in WRITABLE_KINDS:
        return invalid("This calendar is read-only.")
    master = master_for_response(obj)
    merged = merge_patch(master, payload, source["timezone"])
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
