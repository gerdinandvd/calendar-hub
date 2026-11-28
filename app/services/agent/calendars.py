"""Agent calendar listing (enabled sources only)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Source
from app.services.agent.types import WRITABLE_KINDS


def list_agent_calendars(engine: Engine) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(
            select(Source)
            .where(Source.enabled == 1)
            .order_by(Source.created_at, Source.id)
        ).all()
        return [_row(item) for item in rows]


def load_enabled_sources(
    engine: Engine, calendar_ids: list[str] | None = None
) -> list[dict]:
    with Session(engine) as session:
        stmt = select(Source).where(Source.enabled == 1)
        if calendar_ids:
            stmt = stmt.where(Source.id.in_(calendar_ids))
        rows = session.scalars(stmt.order_by(Source.created_at, Source.id)).all()
        return [source_as_dict(item) for item in rows]


def source_as_dict(source: Source) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "kind": source.kind,
        "timezone": source.timezone,
        "collection_path": source.collection_path,
        "enabled": source.enabled,
        "writable": source.kind in WRITABLE_KINDS,
    }


def _row(source: Source) -> dict:
    return {
        "id": source.id,
        "name": source.name,
        "kind": source.kind,
        "timezone": source.timezone,
        "writable": source.kind in WRITABLE_KINDS,
    }
