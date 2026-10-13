"""Shared event writes and mappings. Source modules decide what to write."""

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import NoReturn
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import EventMapping
from app.radicale.client import RadicaleClient, RadicaleError

MAX_EVENTS = 20000
CAPACITY_MESSAGE = "sentence.capacity"
SAVE_MESSAGE = "sentence.event_saved"
_WRITE_WORKERS = 8
_PARALLEL_AT = 32


class RunError(Exception):
    def __init__(self, message: str, stats: "RunStats | None" = None) -> None:
        self.message = message
        self.stats = stats or RunStats()


class Stopped(Exception):
    """The process is stopping. The runner records that sentence."""

    def __init__(self, stats: "RunStats | None" = None) -> None:
        self.stats = stats or RunStats()


@dataclass
class RunStats:
    seen: int = 0
    upserted: int = 0
    deleted: int = 0


@dataclass
class ImportResult:
    stats: RunStats
    cursor: str | None = None
    save_cursor: bool = False


def reraise(exc: RunError | Stopped, stats: RunStats) -> NoReturn:
    """Raise the same failure with the counts gathered so far."""

    if isinstance(exc, Stopped):
        raise Stopped(stats) from None
    raise RunError(exc.message, stats) from None


def event_path(collection_path: str, uid: str) -> str:
    base = collection_path if collection_path.endswith("/") else f"{collection_path}/"
    return f"{base}{quote(uid, safe='')}.ics"


def new_uid() -> str:
    return str(uuid.uuid4())


def load_mapping(
    session: Session, source_id: str, external_id: str
) -> EventMapping | None:
    stmt = select(EventMapping).where(
        EventMapping.source_id == source_id,
        EventMapping.external_id == external_id,
    )
    return session.scalar(stmt)


def remember(
    session: Session,
    source_id: str,
    external_id: str,
    ical_uid: str,
    version: str,
    when: str,
) -> None:
    mapping = load_mapping(session, source_id, external_id)
    if mapping is None:
        session.add(
            EventMapping(
                id=str(uuid.uuid4()),
                source_id=source_id,
                external_id=external_id,
                ical_uid=ical_uid,
                external_version=version,
                last_synced_at=when,
                deleted_at=None,
            )
        )
        return
    mapping.external_version = version
    mapping.last_synced_at = when
    mapping.deleted_at = None


def remember_delete(mapping: EventMapping, when: str) -> None:
    mapping.deleted_at = when
    mapping.last_synced_at = when


def mapping_is_new(engine: Engine, source_id: str, external_id: str) -> bool:
    with Session(engine) as session:
        mapping = load_mapping(session, source_id, external_id)
        return mapping is None or mapping.deleted_at is not None


def live_count(radicale: RadicaleClient, collection: str, stats: RunStats) -> int:
    try:
        return radicale.count_events(collection)
    except RadicaleError:
        raise RunError(SAVE_MESSAGE, stats) from None


def write_parallel(items: list, write: Callable, stop: Callable[[], bool]) -> int:
    if len(items) < _PARALLEL_AT:
        upserted = 0
        for item in items:
            if stop():
                raise Stopped()
            if write(item):
                upserted += 1
        return upserted
    upserted = 0
    workers = min(_WRITE_WORKERS, len(items))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(write, item) for item in items]
        for future in as_completed(futures):
            if future.result():
                upserted += 1
    return upserted


def put_event(radicale: RadicaleClient, path: str, body: bytes, *, fresh: bool) -> None:
    try:
        radicale.put_event(path, body, fresh=fresh)
    except RadicaleError:
        raise RunError(SAVE_MESSAGE) from None


def read_event(radicale: RadicaleClient, path: str) -> bytes | None:
    try:
        return radicale.get_event(path)
    except RadicaleError:
        raise RunError(SAVE_MESSAGE) from None


def delete_event(radicale: RadicaleClient, path: str) -> None:
    try:
        radicale.delete_event(path)
    except RadicaleError:
        raise RunError(SAVE_MESSAGE) from None


def delete_mapped(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    collection: str,
    external_id: str,
) -> bool:
    with Session(engine) as session:
        mapping = load_mapping(session, source_id, external_id)
        if mapping is None or mapping.deleted_at:
            return False
        uid = mapping.ical_uid
    delete_event(radicale, event_path(collection, uid))
    when = utc_now()
    with Session(engine) as session, session.begin():
        mapping = load_mapping(session, source_id, external_id)
        if mapping is not None and mapping.deleted_at is None:
            remember_delete(mapping, when)
    return True


def queue_until_capacity(
    items: list,
    *,
    engine: Engine,
    source_id: str,
    external_id: Callable,
    live: int,
    stop: Callable[[], bool],
) -> tuple[list, bool, int, bool]:
    """Walk items until capacity or stop. Returns (queued, blocked, live, stopped)."""

    queued: list = []
    blocked = False
    for item in items:
        if stop():
            return queued, blocked, live, True
        is_new = mapping_is_new(engine, source_id, external_id(item))
        if is_new and live >= MAX_EVENTS:
            blocked = True
            break
        if is_new:
            live += 1
        queued.append(item)
    return queued, blocked, live, False


def delete_absent_mapped(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    collection: str,
    present: set[str],
    stats: RunStats,
    stop: Callable[[], bool],
) -> None:
    """Delete mapped events whose external_id is missing from a full snapshot."""

    with Session(engine) as session:
        rows = session.scalars(
            select(EventMapping).where(
                EventMapping.source_id == source_id,
                EventMapping.deleted_at.is_(None),
            )
        ).all()
        missing = [row.external_id for row in rows if row.external_id not in present]
    for external_id in missing:
        if stop():
            raise Stopped(stats)
        try:
            removed = delete_mapped(
                engine, radicale, source_id, collection, external_id
            )
        except RunError as exc:
            reraise(exc, stats)
        if removed:
            stats.deleted += 1


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
