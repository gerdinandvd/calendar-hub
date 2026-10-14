"""Create, change, delete, and read sources and their collections."""

import secrets
import uuid
from urllib.parse import quote

from sqlalchemy import delete, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import EventMapping, Source, SourceSecret, SyncState
from app.log import log_mutation
from app.radicale.client import RadicaleClient, RadicaleError
from app.secrets import SecretKeyError, encrypt
from app.services.sources.prepare import (
    _created_poll,
    _GoogleReady,
    _IcsReady,
    _prepare_google,
    _prepare_ics,
    _validate_change,
    _validate_new,
)
from app.services.sources.present import (
    _google_connected,
    _header_set,
    _host,
    _new_secret,
    _new_state,
    _now,
    _owner,
    _public,
    _states,
)
from app.services.sources.types import (
    DEFAULT_TIMEZONE,
    KINDS,
    MAX_SOURCES,
    ConfigError,
    SourceFailure,
    _failure,
    _field,
    _LimitReached,
    _Missing,
)


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


def _count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(Source)) or 0)


def _name_taken(session: Session, name: str, ignore_id: str | None = None) -> bool:
    stmt = select(Source.id).where(Source.name == name)
    if ignore_id is not None:
        stmt = stmt.where(Source.id != ignore_id)
    return session.scalar(stmt) is not None


def _ics_secret(source_id: str, ciphertext: str) -> SourceSecret:
    return SourceSecret(
        source_id=source_id,
        webhook_secret=None,
        oauth_refresh_token=None,
        oauth_access_token=None,
        oauth_expiry=None,
        ics_authorization_header=ciphertext,
    )


def _store_header(session: Session, source_id: str, ciphertext: str | None) -> None:
    secret = session.get(SourceSecret, source_id)
    if secret is None:
        if ciphertext is None:
            return
        session.add(_ics_secret(source_id, ciphertext))
        return
    secret.ics_authorization_header = ciphertext


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
