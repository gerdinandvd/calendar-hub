"""ICS fetch. Parsing stays in ical; Radicale writes stay in the runner."""

import json
import os
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import http.client

from app.sync.ical import CalendarObject, CalendarParseError, parse_vcalendar

MAX_BYTES = 20 * 1024 * 1024
TIMEOUT_SECONDS = 30
MAX_REDIRECTS = 5

FETCH_MESSAGE = "The calendar feed could not be fetched."
TIMEOUT_MESSAGE = "The calendar feed did not respond in time."
SIZE_MESSAGE = "The calendar feed is larger than 20 MB."
READ_MESSAGE = "The calendar feed could not be read."

_REDIRECTS = {301, 302, 303, 307, 308}


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
        if status in _REDIRECTS:
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
    parsed = urlsplit(url)
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
            connection.request("GET", target, headers=headers)
            response = connection.getresponse()
            status = response.status
            found = {name.lower(): value for name, value in response.getheaders()}
            if status in _REDIRECTS or status == 304:
                response.read(65536)
                return status, found, b""
            if status != 200:
                response.read(65536)
                return status, found, b""
            body = _read_limited(response, max_bytes)
        except IcsError:
            raise
        except (TimeoutError, socket.timeout):
            raise IcsError(TIMEOUT_MESSAGE) from None
        except (OSError, http.client.HTTPException):
            raise IcsError(FETCH_MESSAGE) from None
    finally:
        connection.close()
    return status, found, body


def _read_limited(response: http.client.HTTPResponse, max_bytes: int) -> bytes:
    declared = response.getheader("Content-Length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            raise IcsError(FETCH_MESSAGE) from None
        if size > max_bytes:
            raise IcsError(SIZE_MESSAGE)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise IcsError(SIZE_MESSAGE)
        chunks.append(chunk)
    return b"".join(chunks)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text or any(char in text for char in "\r\n"):
        return None
    return text
