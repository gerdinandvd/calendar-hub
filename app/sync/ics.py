"""ICS fetch and import. Parsing stays in ical; event writes go through apply."""

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Source, SourceSecret, SyncState
from app.secrets import SecretKeyError, decrypt
from app.sync.apply import (
    CAPACITY_MESSAGE,
    ImportResult,
    RunError,
    RunStats,
    Stopped,
    delete_absent_mapped,
    event_path,
    live_count,
    load_mapping,
    put_event,
    queue_until_capacity,
    read_event,
    remember,
    reraise,
    utc_now,
    write_parallel,
)
from app.sync.http import REDIRECTS, HttpError, request
from app.sync.ical import CalendarObject, CalendarParseError, build_object, parse_vcalendar

MAX_BYTES = 20 * 1024 * 1024
TIMEOUT_SECONDS = 30
MAX_REDIRECTS = 5

FETCH_MESSAGE = "sentence.feed_fetch"
TIMEOUT_MESSAGE = "sentence.feed_timeout"
SIZE_MESSAGE = "sentence.feed_size"
READ_MESSAGE = "sentence.feed_read"


class IcsError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


@dataclass(frozen=True)
class FeedCursor:
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class NotModified:
    """A conditional GET returned 304. Mappings and events stay as they are."""


@dataclass(frozen=True)
class FeedSnapshot:
    objects: tuple[CalendarObject, ...]
    etag: str | None
    last_modified: str | None


def load_cursor(raw: str | None) -> FeedCursor | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    etag = payload.get("etag")
    modified = payload.get("last_modified")
    if not isinstance(etag, str) or not etag:
        etag = None
    if not isinstance(modified, str) or not modified:
        modified = None
    if etag is None and modified is None:
        return None
    return FeedCursor(etag=etag, last_modified=modified)


def dump_cursor(etag: str | None, last_modified: str | None) -> str | None:
    if not etag and not last_modified:
        return None
    return json.dumps(
        {"etag": etag, "last_modified": last_modified},
        separators=(",", ":"),
    )


def load_feed(
    url: str,
    *,
    allow_http: bool,
    authorization: str | None,
    cursor: FeedCursor | None,
    source_timezone: str,
    timeout: float | None = None,
    max_bytes: int = MAX_BYTES,
) -> FeedSnapshot | NotModified:
    """GET one feed. The body is parsed only after it is complete and within the size limit."""

    if authorization and any(char in authorization for char in "\r\n"):
        raise IcsError(FETCH_MESSAGE)
    if timeout is None:
        timeout = _timeout()
    started = time.monotonic()
    current = _check_url(url, allow_http)
    origin = urlsplit(current)
    hops = 0
    while True:
        left = timeout - (time.monotonic() - started)
        if left <= 0:
            raise IcsError(TIMEOUT_MESSAGE)
        status, headers, body = _once(
            current,
            _headers(current, origin.hostname, authorization, cursor),
            left,
            max_bytes,
        )
        if status in REDIRECTS:
            hops += 1
            location = headers.get("location")
            if hops > MAX_REDIRECTS or not location:
                raise IcsError(FETCH_MESSAGE)
            nxt = urljoin(current, location.strip())
            if urlsplit(nxt).scheme.lower() != origin.scheme.lower():
                raise IcsError(FETCH_MESSAGE)
            current = _check_url(nxt, allow_http)
            continue
        if status == 304:
            return NotModified()
        if status != 200:
            raise IcsError(FETCH_MESSAGE)
        try:
            text = body.decode("utf-8-sig")
            objects = parse_vcalendar(text, source_timezone)
        except (UnicodeError, CalendarParseError):
            raise IcsError(READ_MESSAGE) from None
        return FeedSnapshot(
            objects=objects,
            etag=_clean(headers.get("etag")),
            last_modified=_clean(headers.get("last-modified")),
        )


def _timeout() -> float:
    raw = os.environ.get("HUB_ICS_TIMEOUT", "").strip()
    if not raw:
        return TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return TIMEOUT_SECONDS
    if value <= 0:
        return TIMEOUT_SECONDS
    return value


def _headers(
    url: str,
    origin_host: str | None,
    authorization: str | None,
    cursor: FeedCursor | None,
) -> dict[str, str]:
    headers = {
        "Accept": "text/calendar, text/plain, */*",
        "User-Agent": "Calendar-Hub",
    }
    host = urlsplit(url).hostname
    if authorization and host and origin_host and host.lower() == origin_host.lower():
        headers["Authorization"] = authorization
    if cursor is not None and cursor.etag:
        headers["If-None-Match"] = cursor.etag
    if cursor is not None and cursor.last_modified:
        headers["If-Modified-Since"] = cursor.last_modified
    return headers


def _check_url(url: str, allow_http: bool) -> str:
    text = url.strip()
    if not text or any(char in text for char in "\r\n\t "):
        raise IcsError(FETCH_MESSAGE)
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if parsed.username or parsed.password or not parsed.hostname:
        raise IcsError(FETCH_MESSAGE)
    if scheme == "https" or (scheme == "http" and allow_http):
        return text
    raise IcsError(FETCH_MESSAGE)


def _once(
    url: str,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
) -> tuple[int, dict[str, str], bytes]:
    try:
        return request(
            "GET",
            url,
            headers=headers,
            timeout=timeout,
            max_bytes=max_bytes,
        )
    except HttpError as exc:
        if exc.kind == "timeout":
            raise IcsError(TIMEOUT_MESSAGE) from None
        if exc.kind == "size":
            raise IcsError(SIZE_MESSAGE) from None
        raise IcsError(FETCH_MESSAGE) from None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text or any(char in text for char in "\r\n"):
        return None
    return text


def import_source(engine: Engine, radicale, source_id: str, *, stop: Callable[[], bool]) -> ImportResult:
    """Fetch one feed and write it. The runner stores the cursor after a full success."""

    with Session(engine) as session:
        row = session.get(Source, source_id)
        state = session.get(SyncState, source_id)
        if row is None or state is None:
            raise RunError(FETCH_MESSAGE)
        timezone_name = row.timezone
        url = row.ics_url or ""
        allow_http = row.ics_allow_http == 1
        cursor_raw = state.cursor
        collection = row.collection_path
    try:
        loaded = load_feed(
            url,
            allow_http=allow_http,
            authorization=_authorization(engine, source_id),
            cursor=load_cursor(cursor_raw),
            source_timezone=timezone_name,
        )
    except IcsError as exc:
        raise RunError(exc.message) from None
    if isinstance(loaded, NotModified):
        return ImportResult(RunStats(), None, False)
    if stop():
        raise Stopped()
    stats = _apply_snapshot(engine, radicale, source_id, collection, loaded, stop)
    if stop():
        raise Stopped(stats)
    return ImportResult(stats, dump_cursor(loaded.etag, loaded.last_modified), True)


def _authorization(engine: Engine, source_id: str) -> str | None:
    with Session(engine) as session:
        secret = session.get(SourceSecret, source_id)
        stored = None if secret is None else secret.ics_authorization_header
    if not stored:
        return None
    try:
        return decrypt(stored)
    except SecretKeyError:
        raise RunError(FETCH_MESSAGE) from None


def _apply_snapshot(
    engine: Engine,
    radicale,
    source_id: str,
    collection: str,
    loaded: FeedSnapshot,
    stop: Callable[[], bool],
) -> RunStats:
    stats = RunStats(seen=len(loaded.objects))
    live = live_count(radicale, collection, stats)
    present = {obj.uid for obj in loaded.objects}
    queued, blocked, _live, stopped = queue_until_capacity(
        list(loaded.objects),
        engine=engine,
        source_id=source_id,
        external_id=lambda obj: obj.uid,
        live=live,
        stop=stop,
    )
    if stopped:
        _count_objects(engine, radicale, source_id, collection, queued, stats, stop)
        raise Stopped(stats)
    _count_objects(engine, radicale, source_id, collection, queued, stats, stop)
    if blocked:
        raise RunError(CAPACITY_MESSAGE, stats)
    if stop():
        raise Stopped(stats)
    delete_absent_mapped(engine, radicale, source_id, collection, present, stats, stop)
    return stats


def _count_objects(
    engine: Engine,
    radicale,
    source_id: str,
    collection: str,
    objects: list[CalendarObject],
    stats: RunStats,
    stop: Callable[[], bool],
) -> None:
    if not objects:
        return
    try:
        stats.upserted += write_parallel(
            objects,
            lambda obj: _upsert_object(engine, radicale, source_id, collection, obj)[0],
            stop,
        )
    except (RunError, Stopped) as exc:
        reraise(exc, stats)


def _upsert_object(
    engine: Engine,
    radicale,
    source_id: str,
    collection: str,
    obj: CalendarObject,
) -> tuple[bool, bool]:
    with Session(engine) as session:
        mapping = load_mapping(session, source_id, obj.uid)
        found = mapping is not None
        deleted = mapping.deleted_at if mapping is not None else None
        version = mapping.external_version if mapping is not None else None
        ical_uid = mapping.ical_uid if mapping is not None else obj.uid
    is_new = not found or deleted is not None
    body = build_object(obj).encode("utf-8")
    path = event_path(collection, ical_uid)
    if found and deleted is None and version == obj.version:
        current = read_event(radicale, path)
        if current == body:
            return False, False
    put_event(radicale, path, body, fresh=is_new)
    when = utc_now()
    with Session(engine) as session, session.begin():
        remember(session, source_id, obj.uid, ical_uid, obj.version, when)
    return True, is_new
