"""Create, change, and delete sources, and their collections."""

import os
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.access import json_error
from app.db.models import EventMapping, Source, SourceSecret, SyncState
from app.log import log_mutation
from app.radicale.client import RadicaleClient, RadicaleError
from app.secrets import SecretKeyError, decrypt, encrypt, equal

router = APIRouter()

MAX_SOURCES = 20
DEFAULT_TIMEZONE = "Europe/Amsterdam"
CREATABLE_KINDS = frozenset({"private", "webhook"})
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


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    timezone: str | None = None
    enabled: int | None = None
    kind: str = "private"


class SourcePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    timezone: str | None = None
    enabled: int | None = None


def create_source(
    engine: Engine,
    radicale: RadicaleClient,
    *,
    name: str,
    timezone_name: str | None,
    enabled: int | None,
    kind: str,
    reveal_secret: bool = False,
) -> dict | SourceFailure:
    failure = _validate_new(kind, name, timezone_name, enabled)
    if failure is not None:
        return failure
    cleaned = name.strip()
    zone = (timezone_name or DEFAULT_TIMEZONE).strip()
    bit = 1 if enabled is None else enabled
    plaintext = ""
    ciphertext = None
    if kind == "webhook":
        try:
            plaintext = secrets.token_urlsafe(32)
            ciphertext = encrypt(plaintext)
        except SecretKeyError as exc:
            return _failure(500, str(exc))
    try:
        owner = _owner()
        _host()
    except ConfigError as exc:
        return _failure(500, str(exc))
    with Session(engine) as session:
        if _count(session) >= MAX_SOURCES:
            return _failure(422, "You can add at most 20 sources.")
        if _name_taken(session, cleaned):
            return _field("name", "That name is already in use.")
    source_id = str(uuid.uuid4())
    path = f"/{quote(owner, safe='')}/{source_id}/"
    color = KINDS[kind].color
    try:
        radicale.create_collection(path, cleaned, color)
    except RadicaleError:
        return _failure(500, "The calendar could not be saved.")
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
                poll_seconds=None,
                timezone=zone,
                ics_url=None,
                ics_allow_http=0,
                google_calendar_id=None,
                created_at=stamp,
                updated_at=stamp,
            )
            state = _new_state(source_id, kind)
            session.add(row)
            session.add(state)
            if ciphertext is not None:
                session.add(_new_secret(source_id, ciphertext))
            session.flush()
            payload = _public(row, state)
    except _LimitReached:
        _drop_collection(radicale, path)
        return _failure(422, "You can add at most 20 sources.")
    except IntegrityError:
        _drop_collection(radicale, path)
        return _field("name", "That name is already in use.")
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
) -> dict | SourceFailure:
    failure = _validate_change(name, timezone_name, enabled)
    if failure is not None:
        return failure
    with Session(engine) as session:
        row = session.get(Source, source_id)
        if row is None:
            return _failure(404, "Not found.")
        current_name = row.name
        current_zone = row.timezone
        current_enabled = row.enabled
        current_path = row.collection_path
        current_color = row.color
    new_name = current_name if name is None else name.strip()
    new_zone = current_zone if timezone_name is None else timezone_name.strip()
    new_enabled = current_enabled if enabled is None else enabled
    if new_name != current_name:
        with Session(engine) as session:
            if _name_taken(session, new_name, ignore_id=source_id):
                return _field("name", "That name is already in use.")
        try:
            radicale.update_properties(current_path, new_name, current_color)
        except RadicaleError:
            return _failure(500, "The calendar could not be saved.")
    try:
        with Session(engine) as session, session.begin():
            row = session.get(Source, source_id)
            if row is None:
                raise _Missing()
            row.name = new_name
            row.timezone = new_zone
            row.enabled = new_enabled
            row.updated_at = _now()
            session.flush()
            payload = _public(row, session.get(SyncState, source_id))
    except _Missing:
        if new_name != current_name:
            _restore_name(radicale, current_path, current_name, current_color)
        return _failure(404, "Not found.")
    except IntegrityError:
        if new_name != current_name:
            _restore_name(radicale, current_path, current_name, current_color)
        return _field("name", "That name is already in use.")
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
        return _failure(422, "Deletion requires confirmation.")
    with Session(engine) as session:
        row = session.get(Source, source_id)
        if row is None:
            return _failure(404, "Not found.")
        path = row.collection_path
    try:
        radicale.delete_collection(path)
    except RadicaleError:
        return _failure(500, "The calendar could not be deleted.")
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
        return _public(row, session.get(SyncState, source_id))


def list_sources(engine: Engine) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(select(Source).order_by(Source.created_at, Source.id)).all()
        states = _states(session)
        return [_public(row, states.get(row.id)) for row in rows]


def list_sync_status(engine: Engine) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(select(Source).order_by(Source.created_at, Source.id)).all()
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
            return _failure(404, "Not found.")
        if row.kind != "webhook":
            return _failure(
                422,
                "Only a webhook source can rotate a secret.",
                error="rotate_not_supported",
            )
    try:
        plaintext = secrets.token_urlsafe(32)
        ciphertext = encrypt(plaintext)
    except SecretKeyError as exc:
        return _failure(500, str(exc))
    with Session(engine) as session, session.begin():
        row = session.get(Source, source_id)
        if row is None or row.kind != "webhook":
            return _failure(404, "Not found.")
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
    if not stored:
        equal(provided, "missing-webhook-secret")
        return False
    return equal(decrypt(stored), provided)


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


@router.get("/api/sources")
def read_sources(request: Request) -> dict:
    return {"sources": list_sources(request.app.state.engine)}


@router.post("/api/sources", status_code=201)
def post_source(body: SourceCreate, request: Request) -> dict:
    result = create_source(
        request.app.state.engine,
        request.app.state.radicale,
        name=body.name,
        timezone_name=body.timezone,
        enabled=body.enabled,
        kind=body.kind,
    )
    return _json(result)


@router.get("/api/sources/{source_id}")
def read_source(source_id: str, request: Request) -> dict:
    found = get_source(request.app.state.engine, source_id)
    if found is None:
        return _json(_failure(404, "Not found."))
    return found


@router.patch("/api/sources/{source_id}")
def patch_source(source_id: str, body: SourcePatch, request: Request) -> dict:
    result = update_source(
        request.app.state.engine,
        request.app.state.radicale,
        source_id,
        name=body.name,
        timezone_name=body.timezone,
        enabled=body.enabled,
    )
    return _json(result)


@router.post("/api/sources/{source_id}/rotate-secret")
def rotate_secret(source_id: str, request: Request) -> dict:
    result = rotate_webhook_secret(request.app.state.engine, source_id, reveal=False)
    return _json(result)


@router.delete("/api/sources/{source_id}")
def remove_source(source_id: str, request: Request, confirm: bool = False) -> dict:
    result = delete_source(
        request.app.state.engine,
        request.app.state.radicale,
        source_id,
        confirm=confirm,
    )
    if result is None:
        return {"deleted": True}
    return _json(result)


@router.get("/api/calendars")
def read_calendars(request: Request) -> dict:
    return {"calendars": list_calendars(request.app.state.engine)}


class ConfigError(Exception):
    pass


class _LimitReached(Exception):
    pass


class _Missing(Exception):
    pass


def _json(result: dict | SourceFailure) -> dict:
    if isinstance(result, SourceFailure):
        return json_error(result.status, result.error, result.detail)
    return result


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


def _validate_new(
    kind: str,
    name: str,
    timezone_name: str | None,
    enabled: int | None,
) -> SourceFailure | None:
    if kind not in CREATABLE_KINDS:
        return _failure(422, "Only a private or webhook source can be created.")
    fields: dict[str, str] = {}
    if not name.strip():
        fields["name"] = "Enter a name."
    zone = DEFAULT_TIMEZONE if timezone_name is None else timezone_name.strip()
    if not _timezone_ok(zone):
        fields["timezone"] = "Enter a valid time zone."
    if enabled is not None and enabled not in (0, 1):
        return _failure(422, "Enabled must be 0 or 1.", fields)
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
        fields["name"] = "Enter a name."
    if timezone_name is not None and not _timezone_ok(timezone_name.strip()):
        fields["timezone"] = "Enter a valid time zone."
    if enabled is not None and enabled not in (0, 1):
        return _failure(422, "Enabled must be 0 or 1.", fields)
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


def _status_code(row: Source, state: SyncState | None) -> str:
    if row.kind == "private":
        return "na"
    if state is None:
        return "idle"
    return state.status


def _public(row: Source, state: SyncState | None) -> dict:
    info = KINDS.get(row.kind, KindInfo(row.color, row.color, row.kind))
    status = _status_code(row, state)
    return {
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


def _sync_public(row: Source, state: SyncState | None) -> dict:
    status = _status_code(row, state)
    private = status == "na"
    return {
        "id": row.id,
        "name": row.name,
        "kind": row.kind,
        "kind_label": KINDS.get(row.kind, KindInfo(row.color, row.color, row.kind)).label,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "last_success_at": None if private or state is None else state.last_success_at,
        "last_error_at": None if private or state is None else state.last_error_at,
        "last_error_message": None if private or state is None else state.last_error_message,
        "events_seen": None if private or state is None else state.events_seen,
        "events_upserted": None if private or state is None else state.events_upserted,
        "events_deleted": None if private or state is None else state.events_deleted,
    }


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
