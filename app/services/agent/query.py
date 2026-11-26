"""List/search window, cursor, and collection scanning helpers."""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import EventMapping
from app.radicale.client import RadicaleClient
from app.services.agent.expand import expand_object
from app.services.agent.present import present_occurrence
from app.services.agent.types import (
    DEFAULT_LIST_DAYS,
    MAX_WINDOW_DAYS,
    AgentError,
    invalid,
)
from app.sync.apply import load_mapping_by_uid, utc_now
from app.sync.ical import CalendarEvent, parse_vcalendar
from app.sync.ical_types import CalendarParseError

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CLOCK = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$")


def resolve_list_window(
    window_from: str | None, window_to: str | None
) -> tuple[tuple[datetime, datetime] | None, AgentError | None, dict]:
    applied: dict[str, Any] = {}
    if window_from is None and window_to is None:
        start = datetime.now(UTC)
        end = start + timedelta(days=DEFAULT_LIST_DAYS)
        applied["window"] = "now/+7d"
        return (start, end), None, applied
    return (*parse_window(window_from, window_to), {})


def resolve_search_window(
    window_from: str | None, window_to: str | None
) -> tuple[tuple[datetime, datetime] | None, AgentError | None]:
    if window_from is None and window_to is None:
        return None, None
    return parse_window(window_from, window_to)


def parse_window(
    window_from: str | None, window_to: str | None
) -> tuple[tuple[datetime, datetime] | None, AgentError | None]:
    if window_from is None or window_to is None:
        return None, invalid("from and to are required together.")
    start = parse_bound(window_from)
    end = parse_bound(window_to)
    if isinstance(start, AgentError):
        return None, start
    if isinstance(end, AgentError):
        return None, end
    if end <= start:
        return None, invalid("to must be after from.")
    if end - start > timedelta(days=MAX_WINDOW_DAYS):
        return None, invalid(f"window must be at most {MAX_WINDOW_DAYS} days.")
    return (start, end), None


def parse_bound(value: str) -> datetime | AgentError:
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


def collect_events(
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
            ical_uid = uid_from_href(href)
            try:
                objects = parse_vcalendar(payload.decode("utf-8"), source["timezone"])
            except (UnicodeDecodeError, CalendarParseError):
                continue
            mapping = ensure_mapping_for_uid(engine, source["id"], ical_uid)
            for obj in objects:
                if obj.uid and obj.uid != ical_uid:
                    mapping = ensure_mapping_for_uid(engine, source["id"], obj.uid)
                for item in expand_object(obj, start, end):
                    if needle and not matches(item.event, needle):
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


def matches(event: CalendarEvent, needle: str) -> bool:
    hay = f"{event.summary}\n{event.description}\n{event.location}".lower()
    return needle in hay


def uid_from_href(href: str) -> str:
    name = unquote(href.rstrip("/").rsplit("/", 1)[-1])
    if name.lower().endswith(".ics"):
        name = name[:-4]
    return name


def encode_cursor(offset: int) -> str:
    raw = json.dumps({"o": offset}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str | None) -> int | None:
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


def iso_out(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
