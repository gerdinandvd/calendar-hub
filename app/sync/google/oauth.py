"""Google OAuth tokens and configuration. Read-only calendar scope only."""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from urllib.parse import urlencode

from app.db.models import SourceSecret
from app.secrets import decrypt, encrypt
from app.sync.http import REDIRECTS, HttpError, request

READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
DEFAULT_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_API_URL = "https://www.googleapis.com/calendar/v3"
TIMEOUT_SECONDS = 30
MAX_BYTES = 20 * 1024 * 1024
REFRESH_SKEW_SECONDS = 60

NOT_CONFIGURED = "sentence.google_config"
NOT_CONNECTED = "sentence.google_disconnected"
FETCH_MESSAGE = "sentence.google_fetch"
TOKEN_MESSAGE = "sentence.google_token"
READ_MESSAGE = "sentence.google_read"
SCOPE_MESSAGE = "sentence.google_write"


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


@dataclass(frozen=True)
class TokenBundle:
    refresh_token: str
    access_token: str
    expiry: str


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
    status, payload = json_request("POST", _token_url(), body, TOKEN_MESSAGE, _form_headers())
    return _bundle(status, payload, now, previous_refresh=None, require_refresh=True)


def refresh_if_needed(tokens: TokenBundle, now: datetime) -> TokenBundle:
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
    status, payload = json_request("POST", _token_url(), body, TOKEN_MESSAGE, _form_headers())
    return _bundle(status, payload, now, previous_refresh=tokens.refresh_token, require_refresh=True)


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


def json_request(
    method: str,
    url: str,
    body: bytes | None,
    failure: str,
    headers: dict[str, str],
    timeout: float = TIMEOUT_SECONDS,
    *,
    read_statuses: frozenset[int] | None = None,
) -> tuple[int, object]:
    allowed = read_statuses if read_statuses is not None else frozenset({200})
    try:
        status, _headers, raw = request(
            method,
            url,
            headers=headers,
            body=body,
            timeout=timeout,
            max_bytes=MAX_BYTES,
            read_statuses=allowed,
        )
    except HttpError:
        raise GoogleError(failure) from None
    if status in REDIRECTS:
        raise GoogleError(failure)
    if status == 410 or not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        if status == 200:
            raise GoogleError(failure) from None
        return status, None


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


def _due(tokens: TokenBundle, now: datetime) -> bool:
    if not tokens.access_token:
        return True
    try:
        expiry = datetime.strptime(tokens.expiry, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return expiry <= now + timedelta(seconds=REFRESH_SKEW_SECONDS)


def _same_tokens(secret: SourceSecret, tokens: TokenBundle) -> bool:
    current_refresh = decrypt(secret.oauth_refresh_token) if secret.oauth_refresh_token else ""
    current_access = decrypt(secret.oauth_access_token) if secret.oauth_access_token else ""
    current_expiry = decrypt(secret.oauth_expiry) if secret.oauth_expiry else ""
    return (
        current_refresh == tokens.refresh_token
        and current_access == tokens.access_token
        and current_expiry == tokens.expiry
    )


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


def api_url() -> str:
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
