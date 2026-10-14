"""Sync kickoff and status for sources."""

from urllib.parse import quote

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import EventMapping, Source, SyncState
from app.radicale.client import RadicaleClient, RadicaleError
from app.services.sources.present import (
    _new_state,
    _owner,
    _states,
    _sync_public,
)
from app.services.sources.types import ConfigError, SourceFailure, _failure
from app.sync.runner import SlotBusy, SyncRejected
from app.sync.runner import start_sync as launch_sync


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


def list_sync_status(engine: Engine) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(
            select(Source).order_by(Source.created_at, Source.id)
        ).all()
        states = _states(session)
        return [_sync_public(row, states.get(row.id)) for row in rows]


def ensure_sync_states(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        rows = session.scalars(select(Source)).all()
        existing = set(session.scalars(select(SyncState.source_id)).all())
        for row in rows:
            if row.id in existing:
                continue
            session.add(_new_state(row.id, row.kind))


def migrate_ics_collection_paths(engine: Engine, radicale: RadicaleClient) -> None:
    """Move legacy ICS collections onto the /ics-{id}/ path used for read-only rights."""

    try:
        owner = _owner()
    except ConfigError:
        return
    with Session(engine) as session:
        rows = list(session.scalars(select(Source).where(Source.kind == "ics")).all())
        pending = [
            (
                row.id,
                row.collection_path,
                f"/{quote(owner, safe='')}/ics-{row.id}/",
                row.name,
                row.color,
            )
            for row in rows
            if row.collection_path != f"/{quote(owner, safe='')}/ics-{row.id}/"
        ]
    for source_id, old_path, new_path, name, color in pending:
        try:
            radicale.create_collection(new_path, name, color)
        except RadicaleError:
            try:
                radicale.update_properties(new_path, name, color)
            except RadicaleError:
                continue
        if old_path != new_path:
            try:
                radicale.delete_collection(old_path)
            except RadicaleError:
                pass
        with Session(engine) as session, session.begin():
            row = session.get(Source, source_id)
            if row is None:
                continue
            row.collection_path = new_path
            session.execute(
                delete(EventMapping).where(EventMapping.source_id == source_id)
            )
            state = session.get(SyncState, source_id)
            if state is not None:
                state.cursor = None
