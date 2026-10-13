"""Bounded HTTP for sync fetchers. Callers map errors to their own sentences."""

import http.client
import ssl
from urllib.parse import urlsplit

REDIRECTS = frozenset({301, 302, 303, 307, 308})


class HttpError(Exception):
    """Transport failed. kind is timeout, size, or fetch."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    body: bytes | None = None,
    timeout: float,
    max_bytes: int,
    read_statuses: frozenset[int] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """One HTTP hop. Redirects and 304 return an empty body; the caller decides next."""

    allowed = read_statuses if read_statuses is not None else frozenset({200})
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HttpError("fetch")
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
            found = {name.lower(): value for name, value in response.getheaders()}
            if status in REDIRECTS or status == 304:
                response.read(65536)
                return status, found, b""
            if status not in allowed:
                response.read(65536)
                return status, found, b""
            payload = _read_limited(response, max_bytes)
        except HttpError:
            raise
        except TimeoutError:
            raise HttpError("timeout") from None
        except (OSError, http.client.HTTPException):
            raise HttpError("fetch") from None
    finally:
        connection.close()
    return status, found, payload


def _read_limited(response: http.client.HTTPResponse, max_bytes: int) -> bytes:
    declared = response.getheader("Content-Length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            raise HttpError("fetch") from None
        if size > max_bytes:
            raise HttpError("size")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HttpError("size")
        chunks.append(chunk)
    return b"".join(chunks)
