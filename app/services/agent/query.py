"""List/search window, cursor, and collection scanning helpers."""

from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
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
from app.sync.apply import utc_now
from app.sync.ical import CalendarEvent, parse_vcalendar
from app.sync.ical_types import CalendarObject, CalendarParseError

_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CLOCK = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$")
logger = logging.getLogger("app")


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
) -> tuple[tuple[datetime, datetime] | None, AgentError | None, dict]:
    applied: dict[str, Any] = {}
    if window_from is None and window_to is None:
        end = datetime.now(UTC) + timedelta(days=MAX_WINDOW_DAYS)
        start = datetime.now(UTC) - timedelta(days=MAX_WINDOW_DAYS)
        applied["window"] = "±366d"
        return (start, end), None, applied
    window, error = parse_window(window_from, window_to)
    return window, error, applied


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
    with Session(engine) as session:
        for source in sources:
            hrefs = radicale.list_event_hrefs(source["collection_path"])
            parsed_rows: list[tuple[str, CalendarObject]] = []
            needed: list[str] = []
            for href in hrefs:
                payload = radicale.get_event(href)
                if payload is None:
                    continue
                ical_uid = uid_from_href(href)
                try:
                    objects = parse_vcalendar(
                        payload.decode("utf-8"), source["timezone"]
                    )
                except (UnicodeDecodeError, CalendarParseError):
                    continue
                for obj in objects:
                    uid = obj.uid or ical_uid
                    needed.append(uid)
                    parsed_rows.append((uid, obj))
            cache = _ensure_mappings(session, source["id"], needed)
            for uid, obj in parsed_rows:
                mapping = cache.get(uid)
                if mapping is None:
                    continue
                try:
                    occurrences = expand_object(obj, start, end)
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "agent_expand_skip source_id=%s uid=%s detail=%s",
                        source["id"],
                        obj.uid,
                        type(exc).__name__,
                    )
                    continue
                for item in occurrences:
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


def ensure_mapping_uid_index(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_event_mappings_source_ical_uid "
                "ON event_mappings (source_id, ical_uid)"
            )
        )


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


def _load_mappings(session: Session, source_id: str) -> dict[str, EventMapping]:
    rows = session.scalars(
        select(EventMapping).where(
            EventMapping.source_id == source_id,
            EventMapping.deleted_at.is_(None),
        )
    ).all()
    found: dict[str, EventMapping] = {}
    for row in rows:
        found[row.ical_uid] = row
    return found


def _ensure_mappings(
    session: Session, source_id: str, ical_uids: list[str]
) -> dict[str, EventMapping]:
    cache = _load_mappings(session, source_id)
    missing = [uid for uid in dict.fromkeys(ical_uids) if uid not in cache]
    if not missing:
        return cache
    when = utc_now()
    created: list[EventMapping] = []
    for ical_uid in missing:
        created.append(
            EventMapping(
                id=str(uuid.uuid4()),
                source_id=source_id,
                external_id=f"caldav:{ical_uid}",
                ical_uid=ical_uid,
                external_version=when,
                last_synced_at=when,
                deleted_at=None,
            )
        )
    try:
        session.add_all(created)
        session.commit()
        for row in created:
            cache[row.ical_uid] = row
    except IntegrityError:
        session.rollback()
        cache = _load_mappings(session, source_id)
        still = [uid for uid in missing if uid not in cache]
        if still:
            when = utc_now()
            for ical_uid in still:
                row = EventMapping(
                    id=str(uuid.uuid4()),
                    source_id=source_id,
                    external_id=f"caldav:{ical_uid}",
                    ical_uid=ical_uid,
                    external_version=when,
                    last_synced_at=when,
                    deleted_at=None,
                )
                session.add(row)
                cache[ical_uid] = row
            session.commit()
    return cache
