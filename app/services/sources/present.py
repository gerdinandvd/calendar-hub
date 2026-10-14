"""Read models and DTO builders for sources."""

import os
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Source, SourceSecret, SyncState
from app.services.sources.types import (
    KINDS,
    STATUS_LABELS,
    ConfigError,
    KindInfo,
)


def endpoint_url(source_id: str) -> str:
    return f"https://{_host()}/api/webhooks/{source_id}"


def list_calendars(engine: Engine) -> list[dict]:
    calendars = []
    for item in _list_public(engine):
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


def build_caldav_info(engine: Engine) -> dict:
    """CalDAV connection details for the owner. No password and no internal account."""

    collections = []
    for item in _list_public(engine):
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


def _list_public(engine: Engine) -> list[dict]:
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
            )
            for row in rows
        ]


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


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _header_set(session: Session, source_id: str) -> bool:
    secret = session.get(SourceSecret, source_id)
    return bool(secret is not None and secret.ics_authorization_header)


def _status_code(row: Source, state: SyncState | None) -> str:
    if row.kind == "private":
        return "na"
    if state is None:
        return "idle"
    return state.status


def _public(
    row: Source,
    state: SyncState | None,
    *,
    authorization_set: bool = False,
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
