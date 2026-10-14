"""Normalize and validate source kind fields."""

from dataclasses import dataclass
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.sources.types import (
    CREATABLE_KINDS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_TIMEZONE,
    SourceFailure,
    _failure,
    _field,
)


@dataclass(frozen=True)
class _IcsReady:
    url: str
    allow_http: int
    poll_seconds: int
    header: str | None


def poll_from_form(raw: str) -> int | SourceFailure:
    text = raw.strip()
    if not text.isdigit():
        return _field("poll_seconds", "sentence.poll")
    return _poll_value(int(text))


def _validate_new(
    kind: str,
    name: str,
    timezone_name: str | None,
    enabled: int | None,
    *,
    ics_url: str | None = None,
    ics_allow_http: int | None = None,
    poll_seconds: int | None = None,
    ics_authorization_header: str | None = None,
) -> SourceFailure | None:
    if kind not in CREATABLE_KINDS:
        return _failure(422, "sentence.creatable")
    if kind != "ics" and any(
        value is not None
        for value in (ics_url, ics_allow_http, ics_authorization_header, poll_seconds)
    ):
        return _failure(422, "sentence.fields_ics")
    fields: dict[str, str] = {}
    if not name.strip():
        fields["name"] = "sentence.enter_name"
    zone = DEFAULT_TIMEZONE if timezone_name is None else timezone_name.strip()
    if not _timezone_ok(zone):
        fields["timezone"] = "sentence.timezone"
    if enabled is not None and enabled not in (0, 1):
        return _failure(422, "sentence.enabled", fields)
    if fields:
        return _failure(422, " ".join(fields.values()), fields)
    return None


def _validate_change(
    name: str | None,
    timezone_name: str | None,
    enabled: int | None,
) -> SourceFailure | None:
    fields: dict[str, str] = {}
    if name is not None and not name.strip():
        fields["name"] = "sentence.enter_name"
    if timezone_name is not None and not _timezone_ok(timezone_name.strip()):
        fields["timezone"] = "sentence.timezone"
    if enabled is not None and enabled not in (0, 1):
        return _failure(422, "sentence.enabled", fields)
    if fields:
        return _failure(422, " ".join(fields.values()), fields)
    return None


def _timezone_ok(value: str) -> bool:
    if not value:
        return False
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _prepare_ics(
    url: str | None,
    allow_http: int | None,
    poll_seconds: int | None,
    header: str | None,
) -> _IcsReady | SourceFailure:
    fields: dict[str, str] = {}
    allow = 0 if allow_http is None else allow_http
    if type(allow) is bool or allow not in (0, 1):
        fields["ics_allow_http"] = "sentence.allow_http"
        allow = 0
    text = "" if url is None else url.strip()
    url_error = _url_error(text, allow == 1)
    if url_error:
        fields["ics_url"] = url_error
    poll: int | SourceFailure
    if poll_seconds is None:
        poll = DEFAULT_POLL_SECONDS
    else:
        poll = _poll_value(poll_seconds)
        if isinstance(poll, SourceFailure):
            fields["poll_seconds"] = poll.detail
            poll = DEFAULT_POLL_SECONDS
    cleaned_header, header_error = _clean_header(header)
    if header_error:
        fields["ics_authorization_header"] = header_error
    if fields:
        return _failure(422, " ".join(fields.values()), fields)
    return _IcsReady(
        url=text,
        allow_http=allow,
        poll_seconds=poll if isinstance(poll, int) else DEFAULT_POLL_SECONDS,
        header=cleaned_header,
    )


def _poll_value(value: int) -> int | SourceFailure:
    if type(value) is bool or type(value) is not int or not 1 <= value <= 2147483647:
        return _field("poll_seconds", "sentence.poll")
    return value


def _clean_header(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    text = value.strip()
    if text == "":
        return None, None
    if any(char in text for char in "\r\n") or len(text) > 4096:
        return None, "sentence.authorization"
    return text, None


def _url_error(url: str, allow_http: bool) -> str | None:
    if not url or any(char in url for char in "\r\n\t "):
        return "sentence.https"
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if not parsed.hostname or parsed.username or parsed.password:
        return "sentence.https"
    if scheme == "https" or (scheme == "http" and allow_http):
        return None
    return "sentence.https"


def _created_poll(ready: _IcsReady | None) -> int | None:
    if ready is not None:
        return ready.poll_seconds
    return None
