"""Sync run per source: one slot, status transitions, webhook, ICS, and Google import."""

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import EventMapping, Source, SourceSecret, SyncState
from app.log import log_mutation
from app.radicale.client import RadicaleClient, RadicaleError
from app.secrets import SecretKeyError, decrypt, encrypt
from app.sync import google
from app.sync.ical import CalendarObject, CalendarParseError, build_object, merge_partial, parse_vcalendar
from app.sync.ics import (
    FeedSnapshot,
    IcsError,
    NotModified,
    dump_cursor,
    load_cursor,
    load_feed,
)
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

SHUTDOWN_SECONDS = 30
PROCESS_STOPPED = "The process stopped."
SAVE_MESSAGE = "The event could not be saved."
CAPACITY_MESSAGE = "This calendar already has 20000 events."
MAX_EVENTS = 20000
_WRITE_WORKERS = 8
_PARALLEL_AT = 32
_PULL_KINDS = frozenset({"google", "ics"})

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
            if _STOP.is_set():
                raise SyncFailed(PROCESS_STOPPED, stats)
            _mark_ok(engine, source_id, stats)
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
        return _apply(engine, radicale, source, command)

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
            alive = [source_id for source_id, thread in _INFLIGHT.items() if thread.is_alive()]
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
        raise SyncRejected(404, "not_found", "Not found.")
    if row.kind not in _PULL_KINDS:
        raise SyncRejected(422, "sync_not_supported", "Sync is not used for this source.")
    if row.enabled != 1:
        raise SyncRejected(422, "sync_disabled", "Sync is turned off for this source.")
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
        except SyncFailed as exc:
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


@dataclass
class _Imported:
    stats: RunStats
    cursor: str | None
    save_cursor: bool


def _import_pull(engine: Engine, radicale: RadicaleClient, source_id: str) -> _Imported:
    if _STOP.is_set():
        raise SyncFailed(PROCESS_STOPPED)
    with Session(engine) as session:
        row = session.get(Source, source_id)
        state = session.get(SyncState, source_id)
        if row is None or state is None:
            raise SyncFailed("The calendar feed could not be fetched.")
        kind = row.kind
        timezone_name = row.timezone
        url = row.ics_url or ""
        allow_http = row.ics_allow_http == 1
        cursor_raw = state.cursor
        collection = row.collection_path
        calendar_id = row.google_calendar_id or ""
    if kind == "google":
        return _import_google(
            engine,
            radicale,
            source_id,
            collection,
            calendar_id,
            cursor_raw,
            timezone_name,
        )
    if kind != "ics":
        raise SyncFailed("Sync is not used for this source.")
    try:
        loaded = load_feed(
            url,
            allow_http=allow_http,
            authorization=_ics_authorization(engine, source_id),
            cursor=load_cursor(cursor_raw),
            source_timezone=timezone_name,
        )
    except IcsError as exc:
        raise SyncFailed(exc.message) from None
    if isinstance(loaded, NotModified):
        return _Imported(RunStats(), None, False)
    if _STOP.is_set():
        raise SyncFailed(PROCESS_STOPPED)
    stats = _apply_snapshot(engine, radicale, source_id, collection, loaded)
    if _STOP.is_set():
        raise SyncFailed(PROCESS_STOPPED, stats)
    return _Imported(stats, dump_cursor(loaded.etag, loaded.last_modified), True)


def _import_google(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    collection: str,
    calendar_id: str,
    cursor_raw: str | None,
    timezone_name: str,
) -> _Imported:
    loaded = _google_tokens(engine, source_id)
    if loaded is None:
        raise SyncFailed(google.NOT_CONNECTED)
    try:
        snapshot = google.fetch_events(
            calendar_id=calendar_id,
            cursor=cursor_raw,
            tokens=loaded,
            source_timezone=timezone_name,
            on_invalid_cursor=lambda: _clear_cursor(engine, source_id),
        )
    except google.ScopeRejected as exc:
        raise SyncFailed(exc.message) from None
    except google.GoogleError as exc:
        if exc.invalidate_cursor:
            _clear_cursor(engine, source_id)
        raise SyncFailed(exc.message) from None
    _store_google_tokens(engine, source_id, snapshot.tokens)
    if _STOP.is_set():
        if snapshot.cursor_invalidated:
            _clear_cursor(engine, source_id)
        raise SyncFailed(PROCESS_STOPPED)
    try:
        stats = _apply_google(engine, radicale, source_id, collection, timezone_name, snapshot)
    except SyncFailed:
        if snapshot.cursor_invalidated:
            _clear_cursor(engine, source_id)
        raise
    if _STOP.is_set():
        if snapshot.cursor_invalidated:
            _clear_cursor(engine, source_id)
        raise SyncFailed(PROCESS_STOPPED, stats)
    return _Imported(stats, snapshot.sync_token, True)


def _apply_google(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    collection: str,
    timezone_name: str,
    snapshot: google.GoogleSnapshot,
) -> RunStats:
    stats = RunStats(seen=len(snapshot.changes))
    live = _live_count(radicale, collection, stats)
    present: set[str] = set()
    queued: list[google.GoogleChange] = []
    blocked = False
    for change in snapshot.changes:
        if _STOP.is_set():
            _count_google(engine, radicale, source_id, collection, timezone_name, queued, stats)
            raise SyncFailed(PROCESS_STOPPED, stats)
        if change.cancelled:
            _count_google(engine, radicale, source_id, collection, timezone_name, queued, stats)
            queued = []
            try:
                removed = _delete_external(engine, radicale, source_id, collection, change.external_id)
            except SyncFailed as exc:
                raise SyncFailed(exc.message, stats) from None
            if removed:
                stats.deleted += 1
                live -= 1
            continue
        is_new = _mapping_is_new(engine, source_id, change.external_id)
        if is_new and live >= MAX_EVENTS:
            blocked = True
            break
        if is_new:
            live += 1
        present.add(change.external_id)
        queued.append(change)
    _count_google(engine, radicale, source_id, collection, timezone_name, queued, stats)
    if blocked:
        raise SyncFailed(CAPACITY_MESSAGE, stats)
    if _STOP.is_set():
        raise SyncFailed(PROCESS_STOPPED, stats)
    if snapshot.full:
        _delete_absent(engine, radicale, source_id, collection, present, stats)
    return stats


def _count_google(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    collection: str,
    timezone_name: str,
    changes: list[google.GoogleChange],
    stats: RunStats,
) -> None:
    if not changes:
        return
    try:
        stats.upserted += _write_parallel(
            changes,
            lambda change: _upsert_google(
                engine,
                radicale,
                source_id,
                collection,
                timezone_name,
                change,
                0,
                checked=True,
            )[0],
        )
    except SyncFailed as exc:
        raise SyncFailed(exc.message, stats) from None


def _upsert_google(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    collection: str,
    timezone_name: str,
    change: google.GoogleChange,
    live: int,
    *,
    checked: bool = False,
) -> tuple[bool, bool]:
    with Session(engine) as session:
        mapping = load_mapping(session, source_id, change.external_id)
        found = mapping is not None
        deleted = mapping.deleted_at if mapping is not None else None
        version = mapping.external_version if mapping is not None else None
        if mapping is not None:
            ical_uid = mapping.ical_uid
        elif change.ical_uid:
            ical_uid = change.ical_uid
        else:
            ical_uid = new_uid()
    is_new = not found or deleted is not None
    if not checked and is_new and live >= MAX_EVENTS:
        raise SyncFailed(CAPACITY_MESSAGE)
    if change.replace or not found or deleted is not None:
        obj = _object_from_change(change, ical_uid)
    else:
        obj = _merged_google(radicale, collection, timezone_name, change, ical_uid)
    body = build_object(obj).encode("utf-8")
    path = event_path(collection, ical_uid)
    if found and deleted is None and version == change.etag:
        try:
            current = radicale.get_event(path)
        except RadicaleError:
            raise SyncFailed(SAVE_MESSAGE) from None
        if current == body:
            return False, False
    try:
        radicale.put_event(path, body, fresh=is_new)
    except RadicaleError:
        raise SyncFailed(SAVE_MESSAGE) from None
    when = _now()
    with Session(engine) as session, session.begin():
        _remember_mapping(session, source_id, change.external_id, ical_uid, change.etag, when)
    return True, is_new


def _object_from_change(change: google.GoogleChange, uid: str) -> CalendarObject:
    if change.master is None:
        raise SyncFailed(SAVE_MESSAGE)
    chosen: list = []
    for mark in change.master.exdates + change.added_exdates:
        if not any(mark.value == item.value and mark.all_day == item.all_day for item in chosen):
            chosen.append(mark)
    master = replace(change.master, uid=uid, exdates=tuple(chosen))
    components = [master]
    components.extend(replace(item, uid=uid) for item in change.exceptions)
    return CalendarObject(uid=uid, version=change.etag, components=tuple(components))


def _merged_google(
    radicale: RadicaleClient,
    collection: str,
    timezone_name: str,
    change: google.GoogleChange,
    ical_uid: str,
) -> CalendarObject:
    path = event_path(collection, ical_uid)
    try:
        current = radicale.get_event(path)
    except RadicaleError:
        raise SyncFailed(SAVE_MESSAGE) from None
    if not current:
        return _object_from_change(change, ical_uid)
    try:
        parsed = parse_vcalendar(current.decode("utf-8"), timezone_name)
    except (UnicodeError, CalendarParseError):
        raise SyncFailed(SAVE_MESSAGE) from None
    if len(parsed) != 1:
        raise SyncFailed(SAVE_MESSAGE)
    try:
        return merge_partial(
            parsed[0],
            change.master,
            change.exceptions,
            change.added_exdates,
            uid=ical_uid,
            version=change.etag,
        )
    except CalendarParseError:
        raise SyncFailed(SAVE_MESSAGE) from None


def _delete_absent(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    collection: str,
    present: set[str],
    stats: RunStats,
) -> None:
    with Session(engine) as session:
        rows = session.scalars(
            select(EventMapping).where(
                EventMapping.source_id == source_id,
                EventMapping.deleted_at.is_(None),
            )
        ).all()
        missing = [row.external_id for row in rows if row.external_id not in present]
    for external_id in missing:
        if _STOP.is_set():
            raise SyncFailed(PROCESS_STOPPED, stats)
        try:
            removed = _delete_external(engine, radicale, source_id, collection, external_id)
        except SyncFailed as exc:
            raise SyncFailed(exc.message, stats) from None
        if removed:
            stats.deleted += 1


def _delete_external(
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
    path = event_path(collection, uid)
    try:
        radicale.delete_event(path)
    except RadicaleError:
        raise SyncFailed(SAVE_MESSAGE) from None
    when = _now()
    with Session(engine) as session, session.begin():
        mapping = load_mapping(session, source_id, external_id)
        if mapping is not None and mapping.deleted_at is None:
            remember_delete(mapping, when)
    return True


def _google_tokens(engine: Engine, source_id: str) -> google.TokenBundle | None:
    with Session(engine) as session:
        secret = session.get(SourceSecret, source_id)
        refresh = None if secret is None else secret.oauth_refresh_token
        access = None if secret is None else secret.oauth_access_token
        expiry = None if secret is None else secret.oauth_expiry
    if not refresh:
        return None
    try:
        return google.TokenBundle(
            refresh_token=decrypt(refresh),
            access_token=decrypt(access) if access else "",
            expiry=decrypt(expiry) if expiry else "",
        )
    except SecretKeyError:
        raise SyncFailed(google.FETCH_MESSAGE) from None


def _store_google_tokens(engine: Engine, source_id: str, tokens: google.TokenBundle) -> None:
    with Session(engine) as session, session.begin():
        secret = session.get(SourceSecret, source_id)
        if secret is None:
            session.add(
                SourceSecret(
                    source_id=source_id,
                    webhook_secret=None,
                    oauth_refresh_token=encrypt(tokens.refresh_token),
                    oauth_access_token=encrypt(tokens.access_token),
                    oauth_expiry=encrypt(tokens.expiry),
                    ics_authorization_header=None,
                )
            )
            return
        try:
            current_refresh = decrypt(secret.oauth_refresh_token) if secret.oauth_refresh_token else ""
            current_access = decrypt(secret.oauth_access_token) if secret.oauth_access_token else ""
            current_expiry = decrypt(secret.oauth_expiry) if secret.oauth_expiry else ""
        except SecretKeyError:
            raise SyncFailed(google.FETCH_MESSAGE) from None
        if (
            current_refresh == tokens.refresh_token
            and current_access == tokens.access_token
            and current_expiry == tokens.expiry
        ):
            return
        secret.oauth_refresh_token = encrypt(tokens.refresh_token)
        secret.oauth_access_token = encrypt(tokens.access_token)
        secret.oauth_expiry = encrypt(tokens.expiry)


def _clear_cursor(engine: Engine, source_id: str) -> None:
    with Session(engine) as session, session.begin():
        state = session.get(SyncState, source_id)
        if state is not None and state.status != "na":
            state.cursor = None


def _ics_authorization(engine: Engine, source_id: str) -> str | None:
    with Session(engine) as session:
        secret = session.get(SourceSecret, source_id)
        stored = None if secret is None else secret.ics_authorization_header
    if not stored:
        return None
    try:
        return decrypt(stored)
    except SecretKeyError:
        raise SyncFailed("The calendar feed could not be fetched.") from None


def _apply_snapshot(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    collection: str,
    loaded: FeedSnapshot,
) -> RunStats:
    stats = RunStats(seen=len(loaded.objects))
    live = _live_count(radicale, collection, stats)
    present = {obj.uid for obj in loaded.objects}
    queued: list[CalendarObject] = []
    blocked = False
    for obj in loaded.objects:
        if _STOP.is_set():
            _count_objects(engine, radicale, source_id, collection, queued, stats)
            raise SyncFailed(PROCESS_STOPPED, stats)
        is_new = _mapping_is_new(engine, source_id, obj.uid)
        if is_new and live >= MAX_EVENTS:
            blocked = True
            break
        if is_new:
            live += 1
        queued.append(obj)
    _count_objects(engine, radicale, source_id, collection, queued, stats)
    if blocked:
        raise SyncFailed(CAPACITY_MESSAGE, stats)
    if _STOP.is_set():
        raise SyncFailed(PROCESS_STOPPED, stats)
    with Session(engine) as session:
        rows = session.scalars(
            select(EventMapping).where(
                EventMapping.source_id == source_id,
                EventMapping.deleted_at.is_(None),
            )
        ).all()
        missing = [(row.external_id, row.ical_uid) for row in rows if row.external_id not in present]
    for external_id, ical_uid in missing:
        if _STOP.is_set():
            raise SyncFailed(PROCESS_STOPPED, stats)
        path = event_path(collection, ical_uid)
        try:
            radicale.delete_event(path)
        except RadicaleError:
            raise SyncFailed(SAVE_MESSAGE, stats) from None
        when = _now()
        with Session(engine) as session, session.begin():
            mapping = load_mapping(session, source_id, external_id)
            if mapping is not None and mapping.deleted_at is None:
                remember_delete(mapping, when)
        stats.deleted += 1
        live -= 1
    return stats


def _count_objects(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    collection: str,
    objects: list[CalendarObject],
    stats: RunStats,
) -> None:
    if not objects:
        return
    try:
        stats.upserted += _write_parallel(
            objects,
            lambda obj: _upsert_object(
                engine,
                radicale,
                source_id,
                collection,
                obj,
                0,
                checked=True,
            )[0],
        )
    except SyncFailed as exc:
        raise SyncFailed(exc.message, stats) from None


def _write_parallel(items: list, write) -> int:
    if len(items) < _PARALLEL_AT:
        upserted = 0
        for item in items:
            if _STOP.is_set():
                raise SyncFailed(PROCESS_STOPPED)
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


def _mapping_is_new(engine: Engine, source_id: str, external_id: str) -> bool:
    with Session(engine) as session:
        mapping = load_mapping(session, source_id, external_id)
        return mapping is None or mapping.deleted_at is not None


def _upsert_object(
    engine: Engine,
    radicale: RadicaleClient,
    source_id: str,
    collection: str,
    obj: CalendarObject,
    live: int,
    *,
    checked: bool = False,
) -> tuple[bool, bool]:
    with Session(engine) as session:
        mapping = load_mapping(session, source_id, obj.uid)
        found = mapping is not None
        deleted = mapping.deleted_at if mapping is not None else None
        version = mapping.external_version if mapping is not None else None
        ical_uid = mapping.ical_uid if mapping is not None else obj.uid
    is_new = not found or deleted is not None
    if not checked and is_new and live >= MAX_EVENTS:
        raise SyncFailed(CAPACITY_MESSAGE)
    body = build_object(obj).encode("utf-8")
    path = event_path(collection, ical_uid)
    if found and deleted is None and version == obj.version:
        try:
            current = radicale.get_event(path)
        except RadicaleError:
            raise SyncFailed(SAVE_MESSAGE) from None
        if current == body:
            return False, False
    try:
        radicale.put_event(path, body, fresh=is_new)
    except RadicaleError:
        raise SyncFailed(SAVE_MESSAGE) from None
    when = _now()
    with Session(engine) as session, session.begin():
        _remember_mapping(session, source_id, obj.uid, ical_uid, obj.version, when)
    return True, is_new


def _remember_mapping(
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
        raise SyncFailed(SAVE_MESSAGE, RunStats(seen=1)) from None
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
        is_new = mapping is None or mapping.deleted_at is not None
        uid = mapping.ical_uid if mapping is not None else new_uid()
    if is_new:
        occupied = _live_count(radicale, source["collection_path"], RunStats(seen=1))
        if occupied >= MAX_EVENTS:
            raise SyncFailed(CAPACITY_MESSAGE, RunStats(seen=1))
    body = calendar_bytes(command, uid)
    path = event_path(source["collection_path"], uid)
    try:
        radicale.put_event(path, body, fresh=is_new)
    except RadicaleError:
        raise SyncFailed(SAVE_MESSAGE, RunStats(seen=1)) from None
    when = _now()
    with Session(engine) as session, session.begin():
        remember_upsert(session, source["id"], command, uid, when)
    return RunStats(seen=1, upserted=1)


def _live_count(radicale: RadicaleClient, collection: str, stats: RunStats) -> int:
    try:
        return radicale.count_events(collection)
    except RadicaleError:
        raise SyncFailed(SAVE_MESSAGE, stats) from None


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
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
