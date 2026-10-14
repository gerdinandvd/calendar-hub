"""Sync run per source: one slot, one thread, and the status row."""

import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Source, SyncState
from app.log import log_mutation
from app.radicale.client import RadicaleClient
from app.sync import ics, webhook
from app.sync.apply import SAVE_MESSAGE, ImportResult, RunError, RunStats, Stopped
from app.sync.webhook import Delete, Upsert

SHUTDOWN_SECONDS = 30
PROCESS_STOPPED = "sentence.process_stopped"
_PULL_KINDS = frozenset({"ics"})

_GUARD = threading.Lock()
_SLOTS: dict[str, threading.Lock] = {}
_INFLIGHT: dict[str, threading.Thread] = {}
_STOP = threading.Event()
_FINAL = threading.Lock()


class SlotBusy(Exception):
    """Another run holds this source and the caller chose not to wait."""


class SyncRejected(Exception):
    def __init__(self, status: int, error: str, detail: str) -> None:
        self.status = status
        self.error = error
        self.detail = detail


class SyncFailed(Exception):
    def __init__(self, message: str, stats: RunStats | None = None) -> None:
        self.message = message
        self.stats = stats or RunStats()


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
            if _STOP.is_set():
                raise SyncFailed(PROCESS_STOPPED, stats)
            _mark_ok(engine, source_id, stats)
        except Stopped as exc:
            _mark_error(engine, source_id, PROCESS_STOPPED)
            _log(actor, source_id, run_id, started, "error", exc.stats)
            raise SyncFailed(PROCESS_STOPPED, exc.stats) from None
        except RunError as exc:
            _mark_error(engine, source_id, exc.message)
            _log(actor, source_id, run_id, started, "error", exc.stats)
            raise SyncFailed(exc.message, exc.stats) from None
        except SyncFailed as exc:
            _mark_error(engine, source_id, exc.message)
            _log(actor, source_id, run_id, started, "error", exc.stats)
            raise
        except Exception:
            failed = SyncFailed(SAVE_MESSAGE)
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
        return webhook.apply(engine, radicale, source, command)

    return run_source(engine, source["id"], actor="webhook", work=work, wait=True)


def arm() -> None:
    """Allow runs again. The process calls this when it starts."""

    _STOP.clear()


def start_sync(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    *,
    actor: str,
) -> None:
    """Mark the source running and return. The import continues in the background."""

    _require_pull(engine, source_id)
    lock = _slot(source_id)
    if not lock.acquire(blocking=False):
        raise SlotBusy()
    try:
        _require_pull(engine, source_id)
        _mark_running(engine, source_id, _now())
        _spawn(lock, engine, radicale, source_id, actor)
    except Exception:
        lock.release()
        raise


def stop_runs(engine: Engine, timeout: float = SHUTDOWN_SECONDS) -> None:
    """Wait for background runs, then mark any run that is still going."""

    _STOP.set()
    with _GUARD:
        threads = list(_INFLIGHT.items())
    deadline = time.monotonic() + timeout
    for _source_id, thread in threads:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            thread.join(remaining)
    with _FINAL:
        with _GUARD:
            alive = [
                source_id
                for source_id, thread in _INFLIGHT.items()
                if thread.is_alive()
            ]
        for source_id in alive:
            _mark_error(engine, source_id, PROCESS_STOPPED)


def wait_for(engine: Engine, source_id: str, timeout: float = 5) -> str:
    deadline = time.monotonic() + timeout
    while True:
        with Session(engine) as session:
            state = session.get(SyncState, source_id)
            status = "" if state is None else state.status
        if status != "running":
            return status
        if time.monotonic() >= deadline:
            raise TimeoutError
        time.sleep(0.01)


def _require_pull(engine: Engine, source_id: str) -> None:
    with Session(engine) as session:
        row = session.get(Source, source_id)
        state = session.get(SyncState, source_id)
    if row is None:
        raise SyncRejected(404, "not_found", "sentence.not_found")
    if row.kind not in _PULL_KINDS:
        raise SyncRejected(422, "sync_not_supported", "sentence.sync_unused")
    if row.enabled != 1:
        raise SyncRejected(422, "sync_disabled", "sentence.sync_off")
    if state is not None and state.status == "running":
        raise SlotBusy()


def _spawn(
    lock: threading.Lock,
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    actor: str,
) -> None:
    thread = threading.Thread(
        target=_background,
        args=(lock, engine, radicale, source_id, actor),
        name=f"sync-{source_id}",
        daemon=True,
    )
    with _GUARD:
        _INFLIGHT[source_id] = thread
    thread.start()


def _background(
    lock: threading.Lock,
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    actor: str,
) -> None:
    run_id = str(uuid.uuid4())
    started = _now()
    stats = RunStats()
    outcome = "error"
    try:
        try:
            result = _import_pull(engine, radicale, source_id)
            stats = result.stats
            with _FINAL:
                if _STOP.is_set():
                    _mark_error(engine, source_id, PROCESS_STOPPED)
                else:
                    _mark_ok(
                        engine,
                        source_id,
                        stats,
                        cursor=result.cursor,
                        save_cursor=result.save_cursor,
                    )
                    outcome = "ok"
        except Stopped as exc:
            stats = exc.stats
            with _FINAL:
                _mark_error(engine, source_id, PROCESS_STOPPED)
        except RunError as exc:
            stats = exc.stats
            with _FINAL:
                message = PROCESS_STOPPED if _STOP.is_set() else exc.message
                _mark_error(engine, source_id, message)
        except Exception:
            with _FINAL:
                message = PROCESS_STOPPED if _STOP.is_set() else SAVE_MESSAGE
                _mark_error(engine, source_id, message)
        _log(actor, source_id, run_id, started, outcome, stats)
    finally:
        with _GUARD:
            if _INFLIGHT.get(source_id) is threading.current_thread():
                _INFLIGHT.pop(source_id, None)
        lock.release()


def _import_pull(
    engine: Engine, radicale: RadicaleClient, source_id: str
) -> ImportResult:
    if _STOP.is_set():
        raise Stopped()
    with Session(engine) as session:
        row = session.get(Source, source_id)
        state = session.get(SyncState, source_id)
        if row is None or state is None:
            raise RunError("sentence.feed_fetch")
        kind = row.kind
    if kind != "ics":
        raise RunError("sentence.sync_unused")
    return ics.import_source(engine, radicale, source_id, stop=_STOP.is_set)


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
            raise SyncFailed("sentence.sync_unused")
        state.status = "running"
        state.last_started_at = started


def _mark_ok(
    engine: Engine,
    source_id: str,
    stats: RunStats,
    *,
    cursor: str | None = None,
    save_cursor: bool = False,
) -> None:
    with Session(engine) as session, session.begin():
        state = session.get(SyncState, source_id)
        if state is None or state.status == "na":
            return
        if save_cursor:
            state.cursor = cursor
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
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
