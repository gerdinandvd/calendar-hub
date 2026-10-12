"""Google Calendar import. Fetch stays read-only; import_source writes events through apply."""

import json
import os
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlsplit

import http.client

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import EventMapping, Source, SourceSecret, SyncState
from app.secrets import SecretKeyError, decrypt, encrypt
from app.sync.apply import (
    CAPACITY_MESSAGE,
    MAX_EVENTS,
    SAVE_MESSAGE,
    ImportResult,
    RunError,
    RunStats,
    Stopped,
    delete_mapped,
    event_path,
    live_count,
    load_mapping,
    mapping_is_new,
    new_uid,
    put_event,
    read_event,
    remember,
    reraise,
    utc_now,
    write_parallel,
)
from app.sync.ical import (
    CalendarEvent,
    CalendarObject,
    CalendarParseError,
    Mark,
    build_object,
    google_point,
    merge_partial,
    parse_vcalendar,
    recurrence_parts,
)

READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
DEFAULT_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_API_URL = "https://www.googleapis.com/calendar/v3"
PAGE_SIZE = 250
TIMEOUT_SECONDS = 30
MAX_BYTES = 20 * 1024 * 1024
REFRESH_SKEW_SECONDS = 60
MAX_PAGES = 400

NOT_CONFIGURED = "sentence.google_config"
NOT_CONNECTED = "sentence.google_disconnected"
FETCH_MESSAGE = "sentence.google_fetch"
TOKEN_MESSAGE = "sentence.google_token"
READ_MESSAGE = "sentence.google_read"
SCOPE_MESSAGE = "sentence.google_write"

_REDIRECTS = {301, 302, 303, 307, 308}
_UPDATED = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z$"
)
_STABLE_STAMP = "19700101T000000Z"


class GoogleError(Exception):
    def __init__(self, message: str, *, invalidate_cursor: bool = False) -> None:
        self.message = message
        self.invalidate_cursor = invalidate_cursor
        super().__init__(message)


class ScopeRejected(Exception):
    """The token response granted a scope other than calendar read-only."""

    def __init__(self) -> None:
        self.message = SCOPE_MESSAGE
        super().__init__(self.message)


class _Gone(Exception):
    pass


@dataclass(frozen=True)
class TokenBundle:
    refresh_token: str
    access_token: str
    expiry: str


@dataclass(frozen=True)
class GoogleChange:
    external_id: str
    etag: str
    ical_uid: str | None
    cancelled: bool
    replace: bool
    master: CalendarEvent | None
    exceptions: tuple[CalendarEvent, ...]
    added_exdates: tuple[Mark, ...]


@dataclass(frozen=True)
class GoogleSnapshot:
    full: bool
    changes: tuple[GoogleChange, ...]
    sync_token: str
    tokens: TokenBundle
    cursor_invalidated: bool


def authorization_url(*, state: str, redirect_uri: str) -> str:
    _configured()
    query = urlencode(
        {
            "client_id": _client_id(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": READONLY_SCOPE,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    base = _auth_url()
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{query}"


def exchange_code(*, code: str, redirect_uri: str, now: datetime | None = None) -> TokenBundle:
    _configured()
    if not code or any(char in code for char in "\r\n") or len(code) > 2048:
        raise GoogleError(TOKEN_MESSAGE)
    body = urlencode(
        {
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    status, payload = _json_request("POST", _token_url(), body, TOKEN_MESSAGE, _form_headers())
    return _bundle(status, payload, now, previous_refresh=None, require_refresh=True)


def fetch_events(
    *,
    calendar_id: str,
    cursor: str | None,
    tokens: TokenBundle,
    source_timezone: str,
    on_invalid_cursor: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> GoogleSnapshot:
    """Read every page, then return one snapshot. The caller writes to Radicale."""

    moment = _utc(now)
    if not calendar_id or any(char in calendar_id for char in "\r\n\t "):
        raise GoogleError(FETCH_MESSAGE)
    current = _refresh_if_needed(tokens, moment)
    stored = cursor.strip() if isinstance(cursor, str) else ""
    stored_token = stored or None
    try:
        items, sync_token = _list_all(calendar_id, stored_token, current.access_token)
    except _Gone:
        if on_invalid_cursor is not None:
            on_invalid_cursor()
        try:
            items, sync_token = _list_all(calendar_id, None, current.access_token)
        except GoogleError as exc:
            raise GoogleError(exc.message, invalidate_cursor=True) from None
        return GoogleSnapshot(
            full=True,
            changes=_checked(items, source_timezone, full=True, invalidated=True),
            sync_token=sync_token,
            tokens=current,
            cursor_invalidated=True,
        )
    return GoogleSnapshot(
        full=stored_token is None,
        changes=_checked(items, source_timezone, full=stored_token is None, invalidated=False),
        sync_token=sync_token,
        tokens=current,
        cursor_invalidated=False,
    )


def _checked(
    items: list,
    source_timezone: str,
    *,
    full: bool,
    invalidated: bool,
) -> tuple[GoogleChange, ...]:
    try:
        return _changes(items, source_timezone, full=full)
    except CalendarParseError:
        raise GoogleError(READ_MESSAGE, invalidate_cursor=invalidated) from None
    except GoogleError as exc:
        raise GoogleError(exc.message, invalidate_cursor=invalidated) from None


def _changes(items: list, source_timezone: str, *, full: bool) -> tuple[GoogleChange, ...]:
    if not isinstance(items, list):
        raise GoogleError(READ_MESSAGE)
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise GoogleError(READ_MESSAGE)
        series = item.get("recurringEventId") or item.get("id")
        if not isinstance(series, str) or not series.strip() or any(char in series for char in "\r\n"):
            raise GoogleError(READ_MESSAGE)
        key = series.strip()
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(item)
    return tuple(_one(key, groups[key], source_timezone, full=full) for key in order)


def _one(series_id: str, items: list[dict], source_timezone: str, *, full: bool) -> GoogleChange:
    masters = [item for item in items if not item.get("recurringEventId")]
    others = [item for item in items if item.get("recurringEventId")]
    if any(_cancelled(item) for item in masters):
        master = masters[0]
        return GoogleChange(
            external_id=series_id,
            etag=_etag(master),
            ical_uid=_ical(master),
            cancelled=True,
            replace=True,
            master=None,
            exceptions=(),
            added_exdates=(),
        )
    master_item = masters[0] if masters else None
    ical = _ical(master_item) if master_item is not None else None
    etag = _etag(master_item) if master_item is not None else ""
    exceptions: list[CalendarEvent] = []
    exdates: list[Mark] = []
    for item in others:
        found = _ical(item)
        if ical is None:
            ical = found
        if _cancelled(item):
            exdates.append(_original(item, source_timezone))
            if not etag:
                etag = _etag(item)
            continue
        if not etag:
            etag = _etag(item)
        exceptions.append(
            _component(
                item,
                ical or "pending",
                source_timezone,
                _original(item, source_timezone),
                include_recurrence=False,
            )
        )
    master_event = None
    if master_item is not None:
        master_event = _component(
            master_item,
            ical or "pending",
            source_timezone,
            None,
            include_recurrence=True,
        )
        if exdates:
            master_event = replace(master_event, exdates=_dedupe(master_event.exdates + tuple(exdates)))
    if master_event is None and not exceptions and not exdates:
        raise GoogleError(READ_MESSAGE)
    if (master_event is not None or exceptions) and not etag:
        raise GoogleError(READ_MESSAGE)
    return GoogleChange(
        external_id=series_id,
        etag=etag,
        ical_uid=ical,
        cancelled=False,
        replace=full and master_event is not None,
        master=master_event,
        exceptions=tuple(exceptions),
        added_exdates=() if master_event is not None else tuple(exdates),
    )


def _component(
    item: dict,
    uid: str,
    source_timezone: str,
    recurrence_id: Mark | None,
    *,
    include_recurrence: bool,
) -> CalendarEvent:
    if "start" not in item:
        raise GoogleError(READ_MESSAGE)
    try:
        start = google_point(item.get("start"), source_timezone)
        end = None if item.get("end") is None else google_point(item.get("end"), source_timezone)
        if end is not None and end.all_day != start.all_day:
            raise CalendarParseError()
        parts = (
            recurrence_parts(item.get("recurrence"), source_timezone)
            if include_recurrence and recurrence_id is None
            else recurrence_parts(None, source_timezone)
        )
    except CalendarParseError:
        raise GoogleError(READ_MESSAGE) from None
    return CalendarEvent(
        uid=uid,
        summary=_text(item.get("summary")),
        description=_text(item.get("description")),
        location=_text(item.get("location")),
        status=_vevent_status(item),
        start_value=start.value,
        end_value=None if end is None else end.value,
        all_day=start.all_day,
        timezone=None if start.all_day else start.timezone,
        rrule=None,
        dtstamp=_dtstamp(item),
        rrules=parts.rules,
        exdates=parts.exdates,
        rdates=parts.rdates,
        recurrence_id=recurrence_id,
    )


def _refresh_if_needed(tokens: TokenBundle, now: datetime) -> TokenBundle:
    if not _due(tokens, now):
        return tokens
    _configured()
    body = urlencode(
        {
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "refresh_token": tokens.refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    status, payload = _json_request("POST", _token_url(), body, TOKEN_MESSAGE, _form_headers())
    return _bundle(status, payload, now, previous_refresh=tokens.refresh_token, require_refresh=True)


def _bundle(
    status: int,
    payload: object,
    now: datetime | None,
    *,
    previous_refresh: str | None,
    require_refresh: bool,
) -> TokenBundle:
    if status != 200 or not isinstance(payload, dict):
        raise GoogleError(TOKEN_MESSAGE)
    scope = payload.get("scope")
    if scope is not None and not isinstance(scope, str):
        raise ScopeRejected()
    if not _readonly_scope(scope if isinstance(scope, str) else None):
        raise ScopeRejected()
    access = payload.get("access_token")
    if not isinstance(access, str) or not access or any(char in access for char in "\r\n"):
        raise GoogleError(TOKEN_MESSAGE)
    refresh = payload.get("refresh_token")
    if refresh is None:
        refresh = previous_refresh
    elif not isinstance(refresh, str) or not refresh or any(char in refresh for char in "\r\n"):
        raise GoogleError(TOKEN_MESSAGE)
    if not isinstance(refresh, str) or (require_refresh and not refresh):
        raise GoogleError(TOKEN_MESSAGE)
    expiry = (_utc(now) + timedelta(seconds=_expires_in(payload.get("expires_in")))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return TokenBundle(refresh_token=refresh, access_token=access, expiry=expiry)


def _timeout() -> float:
    raw = os.environ.get("HUB_GOOGLE_TIMEOUT", "").strip()
    if not raw:
        return TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return TIMEOUT_SECONDS
    if value <= 0:
        return TIMEOUT_SECONDS
    return value


def _readonly_scope(scope: str | None) -> bool:
    if scope is None or scope.strip() == "":
        return True
    parts = scope.split()
    return len(parts) == 1 and parts[0] == READONLY_SCOPE


def _due(tokens: TokenBundle, now: datetime) -> bool:
    if not tokens.access_token:
        return True
    try:
        expiry = datetime.strptime(tokens.expiry, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return expiry <= now + timedelta(seconds=REFRESH_SKEW_SECONDS)


def _list_all(
    calendar_id: str,
    sync_token: str | None,
    access_token: str,
) -> tuple[list, str]:
    items: list = []
    page: str | None = None
    seen: set[str] = set()
    deadline = time.monotonic() + _timeout()
    sync_out: str | None = None
    for _page_number in range(MAX_PAGES):
        left = deadline - time.monotonic()
        if left <= 0:
            raise GoogleError(FETCH_MESSAGE)
        status, payload = _json_request(
            "GET",
            _events_url(calendar_id, None if page else sync_token, page),
            None,
            FETCH_MESSAGE,
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": "Calendar-Hub",
            },
            timeout=left,
        )
        if status == 410 and sync_token:
            raise _Gone()
        if status != 200 or not isinstance(payload, dict):
            raise GoogleError(FETCH_MESSAGE)
        batch = payload.get("items", [])
        if batch is None:
            batch = []
        if not isinstance(batch, list):
            raise GoogleError(READ_MESSAGE)
        items.extend(batch)
        next_page = payload.get("nextPageToken")
        if isinstance(next_page, str) and next_page:
            if next_page in seen:
                raise GoogleError(FETCH_MESSAGE)
            seen.add(next_page)
            page = next_page
            continue
        next_sync = payload.get("nextSyncToken")
        if isinstance(next_sync, str) and next_sync and not any(char in next_sync for char in "\r\n"):
            sync_out = next_sync
        break
    else:
        raise GoogleError(FETCH_MESSAGE)
    if not sync_out:
        raise GoogleError(FETCH_MESSAGE)
    return items, sync_out


def _events_url(calendar_id: str, sync_token: str | None, page_token: str | None) -> str:
    params = {"singleEvents": "false", "maxResults": str(PAGE_SIZE)}
    if page_token:
        params["pageToken"] = page_token
    elif sync_token:
        params["syncToken"] = sync_token
    base = _api_url().rstrip("/")
    return f"{base}/calendars/{quote(calendar_id, safe='')}/events?{urlencode(params)}"


def _json_request(
    method: str,
    url: str,
    body: bytes | None,
    failure: str,
    headers: dict[str, str],
    timeout: float = TIMEOUT_SECONDS,
) -> tuple[int, object]:
    status, raw = _request(method, url, headers, body, failure, timeout)
    if status == 410 or not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        if status == 200:
            raise GoogleError(failure) from None
        return status, None


def _request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    failure: str,
    timeout: float,
) -> tuple[int, bytes]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise GoogleError(failure)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            parsed.hostname,
            port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    else:
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
    try:
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            if status in _REDIRECTS:
                response.read(65536)
                raise GoogleError(failure)
            if status != 200 and status != 410:
                response.read(65536)
                return status, b""
            payload = _read_limited(response, failure)
        except GoogleError:
            raise
        except (TimeoutError, socket.timeout):
            raise GoogleError(failure) from None
        except (OSError, http.client.HTTPException):
            raise GoogleError(failure) from None
    finally:
        connection.close()
    return status, payload


def _read_limited(response: http.client.HTTPResponse, failure: str) -> bytes:
    declared = response.getheader("Content-Length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            raise GoogleError(failure) from None
        if size > MAX_BYTES:
            raise GoogleError(failure)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BYTES:
            raise GoogleError(failure)
        chunks.append(chunk)
    return b"".join(chunks)


def _form_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "Calendar-Hub",
    }


def _configured() -> None:
    if not _client_id() or not _client_secret():
        raise GoogleError(NOT_CONFIGURED)


def _client_id() -> str:
    return os.environ.get("HUB_GOOGLE_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.environ.get("HUB_GOOGLE_CLIENT_SECRET", "").strip()


def _auth_url() -> str:
    return os.environ.get("HUB_GOOGLE_AUTH_URL", "").strip() or DEFAULT_AUTH_URL


def _token_url() -> str:
    return os.environ.get("HUB_GOOGLE_TOKEN_URL", "").strip() or DEFAULT_TOKEN_URL


def _api_url() -> str:
    return os.environ.get("HUB_GOOGLE_API_URL", "").strip() or DEFAULT_API_URL


def _expires_in(value: object) -> int:
    if type(value) is int and value > 0:
        return value
    return 3600


def _utc(now: datetime | None) -> datetime:
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _cancelled(item: dict) -> bool:
    status = item.get("status")
    return isinstance(status, str) and status.strip().lower() == "cancelled"


def _etag(item: dict) -> str:
    raw = item.get("etag")
    if not isinstance(raw, str) or not raw.strip() or any(char in raw for char in "\r\n"):
        return ""
    return raw


def _ical(item: dict) -> str | None:
    raw = item.get("iCalUID")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip() or any(char in raw for char in "\r\n"):
        raise GoogleError(READ_MESSAGE)
    return raw.strip()


def _text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise GoogleError(READ_MESSAGE)
    return value


def _vevent_status(item: dict) -> str | None:
    raw = item.get("status")
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if text == "confirmed":
        return "CONFIRMED"
    if text == "tentative":
        return "TENTATIVE"
    return None


def _original(item: dict, source_timezone: str) -> Mark:
    try:
        return google_point(item.get("originalStartTime"), source_timezone)
    except CalendarParseError:
        raise GoogleError(READ_MESSAGE) from None


def _dtstamp(item: dict) -> str:
    raw = item.get("updated")
    if isinstance(raw, str):
        match = _UPDATED.fullmatch(raw.strip())
        if match:
            year, month, day, hour, minute, second = match.groups()
            return f"{year}{month}{day}T{hour}{minute}{second}Z"
    return _STABLE_STAMP


def _dedupe(marks: tuple[Mark, ...]) -> tuple[Mark, ...]:
    chosen: list[Mark] = []
    for mark in marks:
        if not any(mark.value == item.value and mark.all_day == item.all_day for item in chosen):
            chosen.append(mark)
    return tuple(chosen)


def import_source(
    engine: Engine,
    radicale,
    source_id: str,
    *,
    stop: Callable[[], bool],
    on_invalid_cursor: Callable[[], None],
) -> ImportResult:
    """Read one Google calendar and write it. The runner stores the cursor after success."""

    with Session(engine) as session:
        row = session.get(Source, source_id)
        state = session.get(SyncState, source_id)
        if row is None or state is None:
            raise RunError("sentence.feed_fetch")
        collection = row.collection_path
        calendar_id = row.google_calendar_id or ""
        cursor_raw = state.cursor
        timezone_name = row.timezone
    try:
        loaded = load_tokens(engine, source_id)
    except SecretKeyError:
        raise RunError(FETCH_MESSAGE) from None
    if loaded is None:
        raise RunError(NOT_CONNECTED)
    try:
        snapshot = fetch_events(
            calendar_id=calendar_id,
            cursor=cursor_raw,
            tokens=loaded,
            source_timezone=timezone_name,
            on_invalid_cursor=on_invalid_cursor,
        )
    except ScopeRejected as exc:
        raise RunError(exc.message) from None
    except GoogleError as exc:
        if exc.invalidate_cursor:
            on_invalid_cursor()
        raise RunError(exc.message) from None
    try:
        store_tokens(engine, source_id, snapshot.tokens, keep_if_same=True)
    except SecretKeyError:
        raise RunError(FETCH_MESSAGE) from None
    if stop():
        if snapshot.cursor_invalidated:
            on_invalid_cursor()
        raise Stopped()
    try:
        stats = _apply_changes(engine, radicale, source_id, collection, timezone_name, snapshot, stop)
    except (RunError, Stopped):
        if snapshot.cursor_invalidated:
            on_invalid_cursor()
        raise
    if stop():
        if snapshot.cursor_invalidated:
            on_invalid_cursor()
        raise Stopped(stats)
    return ImportResult(stats, snapshot.sync_token, True)


def load_tokens(engine: Engine, source_id: str) -> TokenBundle | None:
    with Session(engine) as session:
        secret = session.get(SourceSecret, source_id)
        refresh = None if secret is None else secret.oauth_refresh_token
        access = None if secret is None else secret.oauth_access_token
        expiry = None if secret is None else secret.oauth_expiry
    if not refresh:
        return None
    return TokenBundle(
        refresh_token=decrypt(refresh),
        access_token=decrypt(access) if access else "",
        expiry=decrypt(expiry) if expiry else "",
    )


def store_tokens(
    engine: Engine,
    source_id: str,
    tokens: TokenBundle,
    *,
    keep_if_same: bool = False,
) -> None:
    """Store OAuth tokens. A sync passes keep_if_same so an unchanged token is left as it is."""

    with Session(engine) as session, session.begin():
        secret = session.get(SourceSecret, source_id)
        if secret is not None and keep_if_same and _same_tokens(secret, tokens):
            return
        refresh = encrypt(tokens.refresh_token)
        access = encrypt(tokens.access_token)
        expiry = encrypt(tokens.expiry)
        if secret is None:
            session.add(
                SourceSecret(
                    source_id=source_id,
                    webhook_secret=None,
                    oauth_refresh_token=refresh,
                    oauth_access_token=access,
                    oauth_expiry=expiry,
                    ics_authorization_header=None,
                )
            )
            return
        secret.oauth_refresh_token = refresh
        secret.oauth_access_token = access
        secret.oauth_expiry = expiry


def _same_tokens(secret: SourceSecret, tokens: TokenBundle) -> bool:
    current_refresh = decrypt(secret.oauth_refresh_token) if secret.oauth_refresh_token else ""
    current_access = decrypt(secret.oauth_access_token) if secret.oauth_access_token else ""
    current_expiry = decrypt(secret.oauth_expiry) if secret.oauth_expiry else ""
    return (
        current_refresh == tokens.refresh_token
        and current_access == tokens.access_token
        and current_expiry == tokens.expiry
    )


def _apply_changes(
    engine: Engine,
    radicale,
    source_id: str,
    collection: str,
    timezone_name: str,
    snapshot: GoogleSnapshot,
    stop: Callable[[], bool],
) -> RunStats:
    stats = RunStats(seen=len(snapshot.changes))
    live = live_count(radicale, collection, stats)
    present: set[str] = set()
    queued: list[GoogleChange] = []
    blocked = False
    for change in snapshot.changes:
        if stop():
            _count_changes(engine, radicale, source_id, collection, timezone_name, queued, stats, stop)
            raise Stopped(stats)
        if change.cancelled:
            _count_changes(engine, radicale, source_id, collection, timezone_name, queued, stats, stop)
            queued = []
            try:
                removed = delete_mapped(engine, radicale, source_id, collection, change.external_id)
            except RunError as exc:
                reraise(exc, stats)
            if removed:
                stats.deleted += 1
                live -= 1
            continue
        is_new = mapping_is_new(engine, source_id, change.external_id)
        if is_new and live >= MAX_EVENTS:
            blocked = True
            break
        if is_new:
            live += 1
        present.add(change.external_id)
        queued.append(change)
    _count_changes(engine, radicale, source_id, collection, timezone_name, queued, stats, stop)
    if blocked:
        raise RunError(CAPACITY_MESSAGE, stats)
    if stop():
        raise Stopped(stats)
    if snapshot.full:
        _delete_absent(engine, radicale, source_id, collection, present, stats, stop)
    return stats


def _count_changes(
    engine: Engine,
    radicale,
    source_id: str,
    collection: str,
    timezone_name: str,
    changes: list[GoogleChange],
    stats: RunStats,
    stop: Callable[[], bool],
) -> None:
    if not changes:
        return
    try:
        stats.upserted += write_parallel(
            changes,
            lambda change: _upsert_change(
                engine,
                radicale,
                source_id,
                collection,
                timezone_name,
                change,
            )[0],
            stop,
        )
    except (RunError, Stopped) as exc:
        reraise(exc, stats)


def _upsert_change(
    engine: Engine,
    radicale,
    source_id: str,
    collection: str,
    timezone_name: str,
    change: GoogleChange,
) -> tuple[bool, bool]:
    with Session(engine) as session:
        mapping = load_mapping(session, source_id, change.external_id)
        found = mapping is not None
        deleted = mapping.deleted_at if mapping is not None else None
        version = mapping.external_version if mapping is not None else None
        if mapping is not None:
            ical_uid = mapping.ical_uid
        elif change.ical_uid:
            ical_uid = change.ical_uid
        else:
            ical_uid = new_uid()
    is_new = not found or deleted is not None
    if change.replace or not found or deleted is not None:
        obj = _object_from_change(change, ical_uid)
    else:
        obj = _merged_change(radicale, collection, timezone_name, change, ical_uid)
    body = build_object(obj).encode("utf-8")
    path = event_path(collection, ical_uid)
    if found and deleted is None and version == change.etag:
        current = read_event(radicale, path)
        if current == body:
            return False, False
    put_event(radicale, path, body, fresh=is_new)
    when = utc_now()
    with Session(engine) as session, session.begin():
        remember(session, source_id, change.external_id, ical_uid, change.etag, when)
    return True, is_new


def _object_from_change(change: GoogleChange, uid: str) -> CalendarObject:
    if change.master is None:
        raise RunError(SAVE_MESSAGE)
    chosen: list = []
    for mark in change.master.exdates + change.added_exdates:
        if not any(mark.value == item.value and mark.all_day == item.all_day for item in chosen):
            chosen.append(mark)
    master = replace(change.master, uid=uid, exdates=tuple(chosen))
    components = [master]
    components.extend(replace(item, uid=uid) for item in change.exceptions)
    return CalendarObject(uid=uid, version=change.etag, components=tuple(components))


def _merged_change(
    radicale,
    collection: str,
    timezone_name: str,
    change: GoogleChange,
    ical_uid: str,
) -> CalendarObject:
    path = event_path(collection, ical_uid)
    current = read_event(radicale, path)
    if not current:
        return _object_from_change(change, ical_uid)
    try:
        parsed = parse_vcalendar(current.decode("utf-8"), timezone_name)
    except (UnicodeError, CalendarParseError):
        raise RunError(SAVE_MESSAGE) from None
    if len(parsed) != 1:
        raise RunError(SAVE_MESSAGE)
    try:
        return merge_partial(
            parsed[0],
            change.master,
            change.exceptions,
            change.added_exdates,
            uid=ical_uid,
            version=change.etag,
        )
    except CalendarParseError:
        raise RunError(SAVE_MESSAGE) from None


def _delete_absent(
    engine: Engine,
    radicale,
    source_id: str,
    collection: str,
    present: set[str],
    stats: RunStats,
    stop: Callable[[], bool],
) -> None:
    with Session(engine) as session:
        rows = session.scalars(
            select(EventMapping).where(
                EventMapping.source_id == source_id,
                EventMapping.deleted_at.is_(None),
            )
        ).all()
        missing = [row.external_id for row in rows if row.external_id not in present]
    for external_id in missing:
        if stop():
            raise Stopped(stats)
        try:
            removed = delete_mapped(engine, radicale, source_id, collection, external_id)
        except RunError as exc:
            reraise(exc, stats)
        if removed:
            stats.deleted += 1
