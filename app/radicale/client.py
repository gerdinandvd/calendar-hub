"""CalDAV collection and event writes. No branch on source kind."""

from __future__ import annotations

import base64
import http.client
import logging
import os
import re
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

logger = logging.getLogger("app")

_STATUS = re.compile(r"HTTP/\d\.\d\s+(\d{3})")


class RadicaleError(Exception):
    """The calendar service could not complete a collection change."""


class RadicaleClient:
    """Creates, renames, and deletes collections for the internal account."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

    @classmethod
    def from_env(cls) -> RadicaleClient:
        return cls(
            base_url=os.environ.get("HUB_RADICALE_URL", "http://radicale:5232"),
            username=os.environ.get("HUB_INTERNAL_USER", ""),
            password=os.environ.get("HUB_INTERNAL_PASSWORD", ""),
        )

    def create_collection(self, path: str, display_name: str, color: str) -> None:
        parent = _parent_path(path)
        if parent:
            # The principal has to exist before a calendar can be created under it.
            self._request("MKCOL", parent, None, ok={200, 201, 204, 405, 409})
        self._request("MKCALENDAR", path, _mkcalendar_body(display_name), ok={200, 201})
        try:
            self.update_properties(path, display_name, color)
        except RadicaleError:
            try:
                self._request("DELETE", path, None, ok={200, 202, 204, 404})
            except RadicaleError:
                logger.error("radicale_error method=DELETE status=cleanup")
            raise

    def update_properties(self, path: str, display_name: str, color: str) -> None:
        self._request(
            "PROPPATCH",
            path,
            _proppatch_body(display_name, color),
            ok={200, 207},
            check_propstat=True,
        )

    def delete_collection(self, path: str) -> None:
        self._request("DELETE", path, None, ok={200, 202, 204, 404})

    def put_event(self, path: str, body: bytes, *, fresh: bool = False) -> None:
        status = self._put(path, body, None if fresh else self._lookup_etag(path))
        if status == 412:
            status = self._put(path, body, self._lookup_etag(path))
        if status not in {200, 201, 204}:
            logger.error("radicale_error method=PUT status=%s", status)
            raise RadicaleError("The calendar could not be saved.")

    def delete_event(self, path: str) -> None:
        self._request("DELETE", path, None, ok={200, 202, 204, 404})

    def get_event(self, path: str) -> bytes | None:
        status, _headers, payload = self._call("GET", path, None)
        if status == 404:
            return None
        if status != 200:
            logger.error("radicale_error method=GET status=%s", status)
            raise RadicaleError("The calendar could not be saved.")
        return payload

    def reachable(self) -> bool:
        """Short probe for the health section. A miss is an error, not a raised failure."""

        try:
            status, _headers, _payload = self._call(
                "PROPFIND",
                "/",
                _event_count_body(),
                depth="0",
                timeout=2,
            )
        except RadicaleError:
            return False
        return status in {200, 207}

    def count_events(self, path: str) -> int:
        status, _headers, payload = self._call(
            "PROPFIND",
            path,
            _event_count_body(),
            depth="1",
        )
        if status not in {200, 207}:
            logger.error("radicale_error method=PROPFIND status=%s", status)
            raise RadicaleError("The calendar could not be saved.")
        return _count_ics_hrefs(payload, path)

    def _lookup_etag(self, path: str) -> str | None:
        status, headers, _payload = self._call("GET", path, None)
        if status == 404:
            return None
        if status != 200:
            logger.error("radicale_error method=GET status=%s", status)
            raise RadicaleError("The calendar could not be saved.")
        return headers.get("etag")

    def _put(self, path: str, body: bytes, etag: str | None) -> int:
        status, _headers, _payload = self._call(
            "PUT",
            path,
            body,
            content_type="text/calendar; charset=utf-8",
            if_match=etag,
        )
        return status

    def _request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        *,
        ok: set[int],
        check_propstat: bool = False,
    ) -> None:
        status, _headers, payload = self._call(method, path, body)
        if status not in ok or (check_propstat and _propstat_failed(payload)):
            logger.error("radicale_error method=%s status=%s", method, status)
            raise RadicaleError("The calendar could not be saved.")

    def _call(
        self,
        method: str,
        path: str,
        body: bytes | None,
        *,
        content_type: str | None = None,
        if_match: str | None = None,
        depth: str | None = None,
        timeout: float = 10,
    ) -> tuple[int, dict[str, str], bytes]:
        parsed = urlsplit(self.base_url)
        if parsed.scheme != "http" or not parsed.hostname:
            logger.error("radicale_error method=%s status=none", method)
            raise RadicaleError("The calendar could not be saved.")
        port = parsed.port or 80
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode(
            "ascii"
        )
        headers = {"Authorization": f"Basic {token}"}
        if body is not None:
            headers["Content-Type"] = content_type or "application/xml; charset=utf-8"
        if if_match:
            headers["If-Match"] = if_match
        if depth:
            headers["Depth"] = depth
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
        try:
            try:
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read()
                status = response.status
                found = {name.lower(): value for name, value in response.getheaders()}
            except OSError:
                logger.error("radicale_error method=%s status=none", method)
                raise RadicaleError("The calendar could not be saved.") from None
        finally:
            connection.close()
        return status, found, payload


def _parent_path(path: str) -> str:
    trimmed = path.strip("/")
    if "/" not in trimmed:
        return ""
    return f"/{trimmed.rsplit('/', 1)[0]}/"


def _mkcalendar_body(display_name: str) -> bytes:
    safe_name = escape(display_name)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        "<D:set><D:prop>"
        f"<D:displayname>{safe_name}</D:displayname>"
        "</D:prop></D:set></C:mkcalendar>"
    )
    return xml.encode("utf-8")


def _proppatch_body(display_name: str, color: str) -> bytes:
    safe_name = escape(display_name)
    safe_color = escape(color)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:propertyupdate xmlns:D="DAV:" xmlns:A="http://apple.com/ns/ical/">'
        "<D:set><D:prop>"
        f"<D:displayname>{safe_name}</D:displayname>"
        f"<A:calendar-color>{safe_color}</A:calendar-color>"
        "</D:prop></D:set></D:propertyupdate>"
    )
    return xml.encode("utf-8")


_HREF = re.compile(
    rb"<(?:[\w.-]+:)?href\b[^>]*>([^<]*)</(?:[\w.-]+:)?href>",
    re.IGNORECASE,
)


def _event_count_body() -> bytes:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:propfind xmlns:D="DAV:"><D:prop><D:getetag/></D:prop></D:propfind>'
    )
    return xml.encode("utf-8")


def _count_ics_hrefs(payload: bytes, collection: str) -> int:
    prefix = collection if collection.endswith("/") else f"{collection}/"
    found: set[str] = set()
    for raw in _HREF.findall(payload):
        text = raw.decode("utf-8", errors="replace").strip()
        path = urlsplit(text).path or text
        if not path.startswith("/"):
            path = prefix + path.lstrip("/")
        if path.startswith(prefix) and path.lower().endswith(".ics"):
            found.add(path)
    return len(found)


def _propstat_failed(payload: bytes) -> bool:
    text = payload.decode("utf-8", errors="replace")
    return any(int(match) >= 400 for match in _STATUS.findall(text))
