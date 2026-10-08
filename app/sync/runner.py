"""Sync run per source: one slot, status transitions, and the webhook write."""

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import SyncState
from app.log import log_mutation
from app.radicale.client import RadicaleClient, RadicaleError
from app.sync.webhook import (
    Delete,
    Upsert,
    calendar_bytes,
    event_path,
    load_mapping,
    new_uid,
    remember_delete,
    remember_upsert,
)

_GUARD = threading.Lock()
_SLOTS: dict[str, threading.Lock] = {}


class SlotBusy(Exception):
    """Another run holds this source and the caller chose not to wait."""


class SyncFailed(Exception):
    def __init__(self, message: str, stats: "RunStats | None" = None) -> None:
        self.message = message
        self.stats = stats or RunStats()


@dataclass
class RunStats:
    seen: int = 0
    upserted: int = 0
    deleted: int = 0


def run_source(
    engine: Engine,
    source_id: str,
    *,
    actor: str,
    work: Callable[[], RunStats],
    wait: bool = True,
) -> RunStats:
    lock = _slot(source_id)
    if not lock.acquire(blocking=wait):
        raise SlotBusy()
    run_id = str(uuid.uuid4())
    started = _now()
    try:
        try:
            _mark_running(engine, source_id, started)
            stats = work()
            _mark_ok(engine, source_id, stats)
        except SyncFailed as exc:
            _mark_error(engine, source_id, exc.message)
            _log(actor, source_id, run_id, started, "error", exc.stats)
            raise
        except Exception:
            failed = SyncFailed("The event could not be saved.")
            _mark_error(engine, source_id, failed.message)
            _log(actor, source_id, run_id, started, "error", failed.stats)
            raise failed from None
        _log(actor, source_id, run_id, started, "ok", stats)
        return stats
    finally:
        lock.release()


def run_webhook(
    engine: Engine,
    radicale: RadicaleClient,
    source: dict,
    command: Upsert | Delete,
) -> RunStats:
    def work() -> RunStats:
        return _apply(engine, radicale, source, command)

    return run_source(engine, source["id"], actor="webhook", work=work, wait=True)


def _apply(
    engine: Engine,
    radicale: RadicaleClient,
    source: dict,
    command: Upsert | Delete,
) -> RunStats:
    if isinstance(command, Delete):
        return _apply_delete(engine, radicale, source, command)
    return _apply_upsert(engine, radicale, source, command)


def _apply_delete(
    engine: Engine,
    radicale: RadicaleClient,
    source: dict,
    command: Delete,
) -> RunStats:
    with Session(engine) as session:
        mapping = load_mapping(session, source["id"], command.external_id)
        if mapping is None or mapping.deleted_at:
            return RunStats(seen=1)
        uid = mapping.ical_uid
    path = event_path(source["collection_path"], uid)
    try:
        radicale.delete_event(path)
    except RadicaleError:
        raise SyncFailed("The event could not be saved.", RunStats(seen=1)) from None
    when = _now()
    with Session(engine) as session, session.begin():
        mapping = load_mapping(session, source["id"], command.external_id)
        if mapping is not None:
            remember_delete(mapping, when)
    return RunStats(seen=1, deleted=1)


def _apply_upsert(
    engine: Engine,
    radicale: RadicaleClient,
    source: dict,
    command: Upsert,
) -> RunStats:
    with Session(engine) as session:
        mapping = load_mapping(session, source["id"], command.external_id)
        if (
            mapping is not None
            and mapping.deleted_at is None
            and mapping.external_version == command.revision
        ):
            return RunStats(seen=1)
        uid = mapping.ical_uid if mapping is not None else new_uid()
    body = calendar_bytes(command, uid)
    path = event_path(source["collection_path"], uid)
    try:
        radicale.put_event(path, body)
    except RadicaleError:
        raise SyncFailed("The event could not be saved.", RunStats(seen=1)) from None
    when = _now()
    with Session(engine) as session, session.begin():
        remember_upsert(session, source["id"], command, uid, when)
    return RunStats(seen=1, upserted=1)


def _slot(source_id: str) -> threading.Lock:
    with _GUARD:
        lock = _SLOTS.get(source_id)
        if lock is None:
            lock = threading.Lock()
            _SLOTS[source_id] = lock
        return lock


def _mark_running(engine: Engine, source_id: str, started: str) -> None:
    with Session(engine) as session, session.begin():
        state = session.get(SyncState, source_id)
        if state is None or state.status == "na":
            raise SyncFailed("Sync is not used for this source.")
        state.status = "running"
        state.last_started_at = started


def _mark_ok(engine: Engine, source_id: str, stats: RunStats) -> None:
    with Session(engine) as session, session.begin():
        state = session.get(SyncState, source_id)
        if state is None or state.status == "na":
            return
        state.status = "ok"
        state.last_success_at = _now()
        state.last_error_message = None
        state.events_seen = stats.seen
        state.events_upserted = stats.upserted
        state.events_deleted = stats.deleted


def _mark_error(engine: Engine, source_id: str, message: str) -> None:
    with Session(engine) as session, session.begin():
        state = session.get(SyncState, source_id)
        if state is None or state.status == "na":
            return
        state.status = "error"
        state.last_error_at = _now()
        state.last_error_message = message


def _log(
    actor: str,
    source_id: str,
    run_id: str,
    started: str,
    outcome: str,
    stats: RunStats,
) -> None:
    log_mutation(
        actor=actor,
        source_id=source_id,
        message=(
            f"run_id={run_id} outcome={outcome} started_at={started} "
            f"finished_at={_now()} events_seen={stats.seen} "
            f"events_upserted={stats.upserted} events_deleted={stats.deleted}"
        ),
    )


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
