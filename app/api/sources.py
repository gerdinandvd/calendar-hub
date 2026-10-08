"""Create, change, and delete private sources, and their collections."""

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.access import json_error
from app.db.models import Source
from app.log import log_mutation
from app.radicale.client import RadicaleClient, RadicaleError

router = APIRouter()

MAX_SOURCES = 20
DEFAULT_TIMEZONE = "Europe/Amsterdam"
STATUS = "na"
STATUS_LABEL = "not applicable"


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
) -> dict | SourceFailure:
    failure = _validate_new(kind, name, timezone_name, enabled)
    if failure is not None:
        return failure
    cleaned = name.strip()
    zone = (timezone_name or DEFAULT_TIMEZONE).strip()
    bit = 1 if enabled is None else enabled
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
    color = KINDS["private"].color
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
                kind="private",
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
            session.add(row)
            session.flush()
            payload = _public(row)
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
            payload = _public(row)
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
        return _public(row)


def list_sources(engine: Engine) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(select(Source).order_by(Source.created_at, Source.id)).all()
        return [_public(row) for row in rows]


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


def _failure(status: int, detail: str, fields: dict[str, str] | None = None) -> SourceFailure:
    found = fields or {}
    if status == 404:
        error = "not_found"
    elif status >= 500:
        error = "internal_error"
    else:
        error = "invalid_request"
    return SourceFailure(status=status, error=error, detail=detail, fields=found)


def _field(name: str, message: str) -> SourceFailure:
    return _failure(422, message, {name: message})


def _validate_new(
    kind: str,
    name: str,
    timezone_name: str | None,
    enabled: int | None,
) -> SourceFailure | None:
    if kind != "private":
        return _failure(422, "Only a private source can be created.")
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


def _public(row: Source) -> dict:
    info = KINDS.get(row.kind, KindInfo(row.color, row.color, row.kind))
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
        "status": STATUS,
        "status_label": STATUS_LABEL,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
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
