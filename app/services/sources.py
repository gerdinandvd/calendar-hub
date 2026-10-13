"""Create, change, and delete sources, and their collections."""

import os
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import EventMapping, Source, SourceSecret, SyncState
from app.log import log_mutation
from app.radicale.client import RadicaleClient, RadicaleError
from app.secrets import SecretKeyError, decrypt, encrypt, equal
from app.sync import google
from app.sync.google import store_tokens
from app.sync.runner import SlotBusy, SyncFailed, SyncRejected, run_webhook
from app.sync.runner import start_sync as launch_sync
from app.sync.webhook import ParseError, parse

MAX_SOURCES = 20
DEFAULT_TIMEZONE = "Europe/Amsterdam"
DEFAULT_POLL_SECONDS = 900
GOOGLE_POLL_SECONDS = 300
CREATABLE_KINDS = frozenset({"private", "webhook", "ics", "google"})
STATUS_LABELS = {
    "idle": "not yet started",
    "running": "busy",
    "ok": "succeeded",
    "error": "error",
    "na": "not applicable",
}


@dataclass(frozen=True)
class KindInfo:
    color: str
    color_name: str
    label: str


KINDS: dict[str, KindInfo] = {
    "private": KindInfo("#1a73e8", "blue", "Private"),
    "google": KindInfo("#188038", "green", "Google"),
    "webhook": KindInfo("#9334e6", "purple", "Webhook"),
    "ics": KindInfo("#f9ab00", "yellow", "External ICS"),
}


@dataclass
class SourceFailure:
    status: int
    error: str
    detail: str
    fields: dict[str, str] = field(default_factory=dict)


class WebhookPayloadError(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail


class WebhookApplyError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class WebhookSecretError(Exception):
    def __init__(self, message: str) -> None:
        self.message = message


class GoogleConnectError(Exception):
    """OAuth connect failed. reason is config, scope, or token."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


@dataclass(frozen=True)
class _IcsReady:
    url: str
    allow_http: int
    poll_seconds: int
    header: str | None


@dataclass(frozen=True)
class _GoogleReady:
    calendar_id: str
    poll_seconds: int


def create_source(
    engine: Engine,
    radicale: RadicaleClient,
    *,
    name: str,
    timezone_name: str | None,
    enabled: int | None,
    kind: str,
    reveal_secret: bool = False,
    ics_url: str | None = None,
    ics_allow_http: int | None = None,
    poll_seconds: int | None = None,
    ics_authorization_header: str | None = None,
    google_calendar_id: str | None = None,
) -> dict | SourceFailure:
    failure = _validate_new(
        kind,
        name,
        timezone_name,
        enabled,
        ics_url=ics_url,
        ics_allow_http=ics_allow_http,
        poll_seconds=poll_seconds,
        ics_authorization_header=ics_authorization_header,
        google_calendar_id=google_calendar_id,
    )
    if failure is not None:
        return failure
    cleaned = name.strip()
    zone = (timezone_name or DEFAULT_TIMEZONE).strip()
    bit = 1 if enabled is None else enabled
    ready: _IcsReady | None = None
    header_cipher = None
    if kind == "ics":
        ready_or_failure = _prepare_ics(
            ics_url,
            ics_allow_http,
            poll_seconds,
            ics_authorization_header,
        )
        if isinstance(ready_or_failure, SourceFailure):
            return ready_or_failure
        ready = ready_or_failure
        if ready.header:
            try:
                header_cipher = encrypt(ready.header)
            except SecretKeyError:
                return _failure(500, "sentence.secret_key")
    google_ready: _GoogleReady | None = None
    if kind == "google":
        prepared = _prepare_google(
            google_calendar_id, poll_seconds, default_calendar=True
        )
        if isinstance(prepared, SourceFailure):
            return prepared
        google_ready = prepared
    plaintext = ""
    ciphertext = None
    if kind == "webhook":
        try:
            plaintext = secrets.token_urlsafe(32)
            ciphertext = encrypt(plaintext)
        except SecretKeyError:
            return _failure(500, "sentence.secret_key")
    try:
        owner = _owner()
        _host()
    except ConfigError as exc:
        return _failure(500, str(exc))
    with Session(engine) as session:
        if _count(session) >= MAX_SOURCES:
            return _failure(422, "sentence.max_sources")
        if _name_taken(session, cleaned):
            return _field("name", "sentence.name_used")
    source_id = str(uuid.uuid4())
    path = f"/{quote(owner, safe='')}/{source_id}/"
    color = KINDS[kind].color
    try:
        radicale.create_collection(path, cleaned, color)
    except RadicaleError:
        return _failure(500, "sentence.calendar_saved")
    stamp = _now()
    try:
        with Session(engine) as session, session.begin():
            if _count(session) >= MAX_SOURCES:
                raise _LimitReached()
            row = Source(
                id=source_id,
                kind=kind,
                name=cleaned,
                color=color,
                collection_path=path,
                enabled=bit,
                poll_seconds=_created_poll(ready, google_ready),
                timezone=zone,
                ics_url=None if ready is None else ready.url,
                ics_allow_http=0 if ready is None else ready.allow_http,
                google_calendar_id=None
                if google_ready is None
                else google_ready.calendar_id,
                created_at=stamp,
                updated_at=stamp,
            )
            state = _new_state(source_id, kind)
            session.add(row)
            session.add(state)
            if ciphertext is not None:
                session.add(_new_secret(source_id, ciphertext))
            elif header_cipher is not None:
                session.add(_ics_secret(source_id, header_cipher))
            session.flush()
            payload = _public(row, state, authorization_set=header_cipher is not None)
    except _LimitReached:
        _drop_collection(radicale, path)
        return _failure(422, "sentence.max_sources")
    except IntegrityError:
        _drop_collection(radicale, path)
        return _field("name", "sentence.name_used")
    except Exception:
        _drop_collection(radicale, path)
        raise
    log_mutation(actor="owner", source_id=source_id, message="created source")
    if reveal_secret and plaintext:
        payload = {**payload, "webhook_secret": plaintext}
    return payload


def update_source(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    *,
    name: str | None = None,
    timezone_name: str | None = None,
    enabled: int | None = None,
    ics_url: str | None = None,
    ics_allow_http: int | None = None,
    poll_seconds: int | None = None,
    ics_authorization_header: str | None = None,
    clear_ics_authorization: bool = False,
    google_calendar_id: str | None = None,
) -> dict | SourceFailure:
    failure = _validate_change(name, timezone_name, enabled)
    if failure is not None:
        return failure
    with Session(engine) as session:
        row = session.get(Source, source_id)
        if row is None:
            return _failure(404, "sentence.not_found")
        current_name = row.name
        current_zone = row.timezone
        current_enabled = row.enabled
        current_path = row.collection_path
        current_color = row.color
        current_kind = row.kind
        current_url = row.ics_url
        current_allow = row.ics_allow_http
        current_poll = row.poll_seconds
        current_calendar = row.google_calendar_id
    ics_change = (
        any(
            value is not None
            for value in (ics_url, ics_allow_http, ics_authorization_header)
        )
        or clear_ics_authorization
    )
    if ics_change and current_kind != "ics":
        return _failure(422, "sentence.fields_ics")
    if poll_seconds is not None and current_kind not in {"ics", "google"}:
        return _failure(422, "sentence.fields_google_or_ics")
    if google_calendar_id is not None and current_kind != "google":
        return _failure(422, "sentence.fields_google")
    ready: _IcsReady | None = None
    header_cipher = None
    header_action = "keep"
    if current_kind == "ics" and (ics_change or poll_seconds is not None):
        ready_or_failure = _prepare_ics(
            current_url if ics_url is None else ics_url,
            current_allow if ics_allow_http is None else ics_allow_http,
            current_poll if poll_seconds is None else poll_seconds,
            ics_authorization_header,
        )
        if isinstance(ready_or_failure, SourceFailure):
            return ready_or_failure
        ready = ready_or_failure
        if clear_ics_authorization and not ready.header:
            header_action = "clear"
        elif ready.header:
            header_action = "replace"
            try:
                header_cipher = encrypt(ready.header)
            except SecretKeyError:
                return _failure(500, "sentence.secret_key")
    google_ready: _GoogleReady | None = None
    if current_kind == "google" and (
        google_calendar_id is not None or poll_seconds is not None
    ):
        prepared = _prepare_google(
            current_calendar if google_calendar_id is None else google_calendar_id,
            current_poll if poll_seconds is None else poll_seconds,
            default_calendar=False,
        )
        if isinstance(prepared, SourceFailure):
            return prepared
        google_ready = prepared
    new_name = current_name if name is None else name.strip()
    new_zone = current_zone if timezone_name is None else timezone_name.strip()
    new_enabled = current_enabled if enabled is None else enabled
    if new_name != current_name:
        with Session(engine) as session:
            if _name_taken(session, new_name, ignore_id=source_id):
                return _field("name", "sentence.name_used")
        try:
            radicale.update_properties(current_path, new_name, current_color)
        except RadicaleError:
            return _failure(500, "sentence.calendar_saved")
    try:
        with Session(engine) as session, session.begin():
            row = session.get(Source, source_id)
            if row is None:
                raise _Missing()
            row.name = new_name
            row.timezone = new_zone
            row.enabled = new_enabled
            row.updated_at = _now()
            if ready is not None:
                row.ics_url = ready.url
                row.ics_allow_http = ready.allow_http
                row.poll_seconds = ready.poll_seconds
            if google_ready is not None:
                row.google_calendar_id = google_ready.calendar_id
                row.poll_seconds = google_ready.poll_seconds
            drop_cursor = new_zone != current_zone or (
                ready is not None and ready.url != current_url
            )
            if (
                google_ready is not None
                and google_ready.calendar_id != current_calendar
            ):
                drop_cursor = True
            if header_action != "keep":
                _store_header(
                    session,
                    source_id,
                    None if header_action == "clear" else header_cipher,
                )
                drop_cursor = True
            if drop_cursor and row.kind in {"ics", "google"}:
                state = session.get(SyncState, source_id)
                if state is not None:
                    state.cursor = None
            session.flush()
            payload = _public(
                row,
                session.get(SyncState, source_id),
                authorization_set=_header_set(session, source_id),
                google_connected=_google_connected(session, source_id),
            )
    except _Missing:
        if new_name != current_name:
            _restore_name(radicale, current_path, current_name, current_color)
        return _failure(404, "sentence.not_found")
    except IntegrityError:
        if new_name != current_name:
            _restore_name(radicale, current_path, current_name, current_color)
        return _field("name", "sentence.name_used")
    except Exception:
        if new_name != current_name:
            _restore_name(radicale, current_path, current_name, current_color)
        raise
    log_mutation(actor="owner", source_id=source_id, message="updated source")
    return payload


def delete_source(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    *,
    confirm: bool,
) -> None | SourceFailure:
    if not confirm:
        return _failure(422, "sentence.confirm_delete")
    with Session(engine) as session:
        row = session.get(Source, source_id)
        if row is None:
            return _failure(404, "sentence.not_found")
        path = row.collection_path
    try:
        radicale.delete_collection(path)
    except RadicaleError:
        return _failure(500, "sentence.calendar_deleted")
    with Session(engine) as session, session.begin():
        session.execute(delete(EventMapping).where(EventMapping.source_id == source_id))
        session.execute(delete(SourceSecret).where(SourceSecret.source_id == source_id))
        session.execute(delete(SyncState).where(SyncState.source_id == source_id))
        row = session.get(Source, source_id)
        if row is not None:
            session.delete(row)
    log_mutation(actor="owner", source_id=source_id, message="deleted source")
    return None


def get_source(engine: Engine, source_id: str) -> dict | None:
    with Session(engine) as session:
        row = session.get(Source, source_id)
        if row is None:
            return None
        return _public(
            row,
            session.get(SyncState, source_id),
            authorization_set=_header_set(session, source_id),
            google_connected=_google_connected(session, source_id),
        )


def list_sources(engine: Engine) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(
            select(Source).order_by(Source.created_at, Source.id)
        ).all()
        states = _states(session)
        return [
            _public(
                row,
                states.get(row.id),
                authorization_set=_header_set(session, row.id),
                google_connected=_google_connected(session, row.id),
            )
            for row in rows
        ]


def start_sync(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    *,
    actor: str = "owner",
) -> dict | SourceFailure:
    try:
        launch_sync(engine, radicale, source_id, actor=actor)
    except SlotBusy:
        return _failure(409, "sentence.sync_running", error="sync_running")
    except SyncRejected as exc:
        return _failure(exc.status, exc.detail, error=exc.error)
    return {"status": "running"}


def apply_webhook_payload(
    engine: Engine,
    radicale: RadicaleClient,
    source: dict,
    payload: object,
) -> None:
    """Parse a webhook body and apply it. Raises WebhookPayloadError or WebhookApplyError."""

    try:
        command = parse(payload, source["timezone"])
    except ParseError as exc:
        raise WebhookPayloadError(exc.detail) from None
    try:
        run_webhook(engine, radicale, source, command)
    except SyncFailed as exc:
        raise WebhookApplyError(exc.message) from None


def google_callback_uri() -> str:
    host = os.environ.get("HUB_HOST", "").strip()
    client = os.environ.get("HUB_GOOGLE_CLIENT_ID", "").strip()
    secret = os.environ.get("HUB_GOOGLE_CLIENT_SECRET", "").strip()
    if not host or not client or not secret:
        raise GoogleConnectError("config")
    return f"https://{host}/api/oauth/google/callback"


def google_authorization_url(*, state: str) -> str:
    try:
        return google.authorization_url(
            state=state,
            redirect_uri=google_callback_uri(),
        )
    except google.GoogleError as exc:
        if exc.message == google.NOT_CONFIGURED:
            raise GoogleConnectError("config") from None
        raise GoogleConnectError("token") from None


def connect_google_source(engine: Engine, source_id: str, *, code: str) -> None:
    try:
        redirect_uri = google_callback_uri()
        bundle = google.exchange_code(code=code, redirect_uri=redirect_uri)
        store_tokens(engine, source_id, bundle)
    except GoogleConnectError:
        raise
    except google.ScopeRejected:
        raise GoogleConnectError("scope") from None
    except (google.GoogleError, SecretKeyError):
        raise GoogleConnectError("token") from None
    log_mutation(actor="owner", source_id=source_id, message="connected google source")


def list_sync_status(engine: Engine) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(
            select(Source).order_by(Source.created_at, Source.id)
        ).all()
        states = _states(session)
        return [_sync_public(row, states.get(row.id)) for row in rows]


def rotate_webhook_secret(
    engine: Engine,
    source_id: str,
    *,
    reveal: bool = False,
) -> dict | SourceFailure:
    with Session(engine) as session:
        row = session.get(Source, source_id)
        if row is None:
            return _failure(404, "sentence.not_found")
        if row.kind != "webhook":
            return _failure(
                422,
                "sentence.rotate_webhook",
                error="rotate_not_supported",
            )
    try:
        plaintext = secrets.token_urlsafe(32)
        ciphertext = encrypt(plaintext)
    except SecretKeyError:
        return _failure(500, "sentence.secret_key")
    with Session(engine) as session, session.begin():
        row = session.get(Source, source_id)
        if row is None or row.kind != "webhook":
            return _failure(404, "sentence.not_found")
        secret = session.get(SourceSecret, source_id)
        if secret is None:
            secret = _new_secret(source_id, ciphertext)
            session.add(secret)
        else:
            secret.webhook_secret = ciphertext
        row.updated_at = _now()
    log_mutation(actor="owner", source_id=source_id, message="rotated webhook secret")
    payload: dict = {"id": source_id, "rotated": True}
    if reveal:
        payload["webhook_secret"] = plaintext
    return payload


def webhook_secret_matches(engine: Engine, source_id: str, provided: str) -> bool:
    with Session(engine) as session:
        row = session.get(SourceSecret, source_id)
        stored = row.webhook_secret if row is not None else None
    try:
        if not stored:
            equal(provided, "missing-webhook-secret")
            return False
        return equal(decrypt(stored), provided)
    except SecretKeyError as exc:
        raise WebhookSecretError(str(exc)) from None


def endpoint_url(source_id: str) -> str:
    return f"https://{_host()}/api/webhooks/{source_id}"


def ensure_sync_states(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        rows = session.scalars(select(Source)).all()
        existing = set(session.scalars(select(SyncState.source_id)).all())
        for row in rows:
            if row.id in existing:
                continue
            session.add(_new_state(row.id, row.kind))


def list_calendars(engine: Engine) -> list[dict]:
    calendars = []
    for item in list_sources(engine):
        calendars.append(
            {
                "name": item["name"],
                "color": item["color"],
                "color_name": item["color_name"],
                "collection_url": item["collection_url"],
                "kind": item["kind"],
                "kind_label": item["kind_label"],
            }
        )
    return calendars


class ConfigError(Exception):
    pass


class _LimitReached(Exception):
    pass


class _Missing(Exception):
    pass


def _failure(
    status: int,
    detail: str,
    fields: dict[str, str] | None = None,
    *,
    error: str | None = None,
) -> SourceFailure:
    found = fields or {}
    if error is not None:
        code = error
    elif status == 404:
        code = "not_found"
    elif status >= 500:
        code = "internal_error"
    else:
        code = "invalid_request"
    return SourceFailure(status=status, error=code, detail=detail, fields=found)


def _field(name: str, message: str) -> SourceFailure:
    return _failure(422, message, {name: message})


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
    google_calendar_id: str | None = None,
) -> SourceFailure | None:
    if kind not in CREATABLE_KINDS:
        return _failure(422, "sentence.creatable")
    if kind != "ics" and any(
        value is not None
        for value in (ics_url, ics_allow_http, ics_authorization_header)
    ):
        return _failure(422, "sentence.fields_ics")
    if kind not in {"ics", "google"} and poll_seconds is not None:
        return _failure(422, "sentence.fields_google_or_ics")
    if kind != "google" and google_calendar_id is not None:
        return _failure(422, "sentence.fields_google")
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


def _owner() -> str:
    user = os.environ.get("HUB_USER", "").strip()
    if not user:
        raise ConfigError("The owner account is not configured.")
    return user


def _host() -> str:
    host = os.environ.get("HUB_HOST", "").strip()
    if not host:
        raise ConfigError("The public host is not configured.")
    return host


def _count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(Source)) or 0)


def _name_taken(session: Session, name: str, ignore_id: str | None = None) -> bool:
    stmt = select(Source.id).where(Source.name == name)
    if ignore_id is not None:
        stmt = stmt.where(Source.id != ignore_id)
    return session.scalar(stmt) is not None


def _states(session: Session) -> dict[str, SyncState]:
    rows = session.scalars(select(SyncState)).all()
    return {row.source_id: row for row in rows}


def _new_state(source_id: str, kind: str) -> SyncState:
    return SyncState(
        source_id=source_id,
        status="na" if kind == "private" else "idle",
        cursor=None,
        last_started_at=None,
        last_success_at=None,
        last_error_at=None,
        last_error_message=None,
        events_seen=0,
        events_upserted=0,
        events_deleted=0,
    )


def _new_secret(source_id: str, ciphertext: str) -> SourceSecret:
    return SourceSecret(
        source_id=source_id,
        webhook_secret=ciphertext,
        oauth_refresh_token=None,
        oauth_access_token=None,
        oauth_expiry=None,
        ics_authorization_header=None,
    )


def _ics_secret(source_id: str, ciphertext: str) -> SourceSecret:
    return SourceSecret(
        source_id=source_id,
        webhook_secret=None,
        oauth_refresh_token=None,
        oauth_access_token=None,
        oauth_expiry=None,
        ics_authorization_header=ciphertext,
    )


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


def _store_header(session: Session, source_id: str, ciphertext: str | None) -> None:
    secret = session.get(SourceSecret, source_id)
    if secret is None:
        if ciphertext is None:
            return
        session.add(_ics_secret(source_id, ciphertext))
        return
    secret.ics_authorization_header = ciphertext


def _header_set(session: Session, source_id: str) -> bool:
    secret = session.get(SourceSecret, source_id)
    return bool(secret is not None and secret.ics_authorization_header)


def _status_code(row: Source, state: SyncState | None) -> str:
    if row.kind == "private":
        return "na"
    if state is None:
        return "idle"
    return state.status


def _created_poll(
    ready: _IcsReady | None, google_ready: _GoogleReady | None
) -> int | None:
    if ready is not None:
        return ready.poll_seconds
    if google_ready is not None:
        return google_ready.poll_seconds
    return None


def _prepare_google(
    calendar_id: str | None,
    poll_seconds: int | None,
    *,
    default_calendar: bool,
) -> _GoogleReady | SourceFailure:
    fields: dict[str, str] = {}
    if calendar_id is None or (default_calendar and not calendar_id.strip()):
        calendar = "primary"
    else:
        calendar = calendar_id.strip()
        error = _calendar_error(calendar)
        if error:
            fields["google_calendar_id"] = error
    poll: int | SourceFailure
    if poll_seconds is None:
        poll = GOOGLE_POLL_SECONDS
    else:
        poll = _poll_value(poll_seconds)
        if isinstance(poll, SourceFailure):
            fields["poll_seconds"] = poll.detail
    if fields:
        return _failure(422, " ".join(fields.values()), fields)
    return _GoogleReady(
        calendar_id=calendar if "google_calendar_id" not in fields else "primary",
        poll_seconds=poll if isinstance(poll, int) else GOOGLE_POLL_SECONDS,
    )


def _calendar_error(value: str) -> str | None:
    if not value or any(char in value for char in "\r\n\t "):
        return "sentence.calendar_id"
    if len(value) > 1024:
        return "sentence.calendar_id"
    return None


def _google_connected(session: Session, source_id: str) -> bool:
    secret = session.get(SourceSecret, source_id)
    return bool(secret is not None and secret.oauth_refresh_token)


def _public(
    row: Source,
    state: SyncState | None,
    *,
    authorization_set: bool = False,
    google_connected: bool = False,
) -> dict:
    info = KINDS.get(row.kind, KindInfo(row.color, row.color, row.kind))
    status = _status_code(row, state)
    payload = {
        "id": row.id,
        "kind": row.kind,
        "kind_label": info.label,
        "name": row.name,
        "color": row.color,
        "color_name": info.color_name,
        "collection_path": row.collection_path,
        "collection_url": f"https://{_host()}/radicale{row.collection_path}",
        "enabled": row.enabled,
        "timezone": row.timezone,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if row.kind == "ics":
        payload["ics_url"] = row.ics_url
        payload["poll_seconds"] = row.poll_seconds
        payload["ics_allow_http"] = row.ics_allow_http
        payload["ics_authorization_set"] = authorization_set
    if row.kind == "google":
        payload["google_calendar_id"] = row.google_calendar_id
        payload["poll_seconds"] = row.poll_seconds
        payload["google_connected"] = google_connected
    return payload


def _sync_public(row: Source, state: SyncState | None) -> dict:
    status = _status_code(row, state)
    private = status == "na"
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "kind_label": KINDS.get(
            row.kind, KindInfo(row.color, row.color, row.kind)
        ).label,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "last_success_at": None if private or state is None else state.last_success_at,
        "last_error_at": None if private or state is None else state.last_error_at,
        "last_error_message": None
        if private or state is None
        else state.last_error_message,
        "events_seen": None if private or state is None else state.events_seen,
        "events_upserted": None if private or state is None else state.events_upserted,
        "events_deleted": None if private or state is None else state.events_deleted,
    }


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _drop_collection(radicale: RadicaleClient, path: str) -> None:
    try:
        radicale.delete_collection(path)
    except RadicaleError:
        return


def _restore_name(radicale: RadicaleClient, path: str, name: str, color: str) -> None:
    try:
        radicale.update_properties(path, name, color)
    except RadicaleError:
        return


def build_caldav_info(engine: Engine) -> dict:
    """CalDAV connection details for the owner. No password and no internal account."""

    collections = []
    for item in list_sources(engine):
        collections.append(
            {
                "name": item["name"],
                "url": item["collection_url"],
                "color": item["color"],
                "color_name": item["color_name"],
            }
        )
    return {
        "host": os.environ.get("HUB_HOST", "").strip(),
        "username": os.environ.get("HUB_USER", "").strip(),
        "collections": collections,
    }
