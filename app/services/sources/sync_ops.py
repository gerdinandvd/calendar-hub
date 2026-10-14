"""Sync kickoff and status for sources."""

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Source, SyncState
from app.radicale.client import RadicaleClient
from app.services.sources.present import _new_state, _states, _sync_public
from app.services.sources.types import SourceFailure, _failure
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
