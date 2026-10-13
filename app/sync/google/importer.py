"""Apply a Google snapshot to Radicale and mappings."""

from collections.abc import Callable
from dataclasses import replace

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.models import Source, SyncState
from app.secrets import SecretKeyError
from app.sync.apply import (
    CAPACITY_MESSAGE,
    MAX_EVENTS,
    SAVE_MESSAGE,
    ImportResult,
    RunError,
    RunStats,
    Stopped,
    delete_absent_mapped,
    delete_mapped,
    event_path,
    live_count,
    load_mapping,
    mapping_is_new,
    new_uid,
    put_event,
    read_event,
    remember,
    reraise,
    utc_now,
    write_parallel,
)
from app.sync.google.fetch import GoogleChange, GoogleSnapshot, fetch_events
from app.sync.google.oauth import (
    FETCH_MESSAGE,
    NOT_CONNECTED,
    GoogleError,
    ScopeRejected,
    load_tokens,
    store_tokens,
)
from app.sync.ical import (
    CalendarObject,
    CalendarParseError,
    build_object,
    merge_partial,
    parse_vcalendar,
)


def import_source(
    engine: Engine,
    radicale,
    source_id: str,
    *,
    stop: Callable[[], bool],
    on_invalid_cursor: Callable[[], None],
) -> ImportResult:
    """Read one Google calendar and write it. The runner stores the cursor after success."""

    with Session(engine) as session:
        row = session.get(Source, source_id)
        state = session.get(SyncState, source_id)
        if row is None or state is None:
            raise RunError("sentence.feed_fetch")
        collection = row.collection_path
        calendar_id = row.google_calendar_id or ""
        cursor_raw = state.cursor
        timezone_name = row.timezone
    try:
        loaded = load_tokens(engine, source_id)
    except SecretKeyError:
        raise RunError(FETCH_MESSAGE) from None
    if loaded is None:
        raise RunError(NOT_CONNECTED)
    try:
        snapshot = fetch_events(
            calendar_id=calendar_id,
            cursor=cursor_raw,
            tokens=loaded,
            source_timezone=timezone_name,
            on_invalid_cursor=on_invalid_cursor,
        )
    except ScopeRejected as exc:
        raise RunError(exc.message) from None
    except GoogleError as exc:
        if exc.invalidate_cursor:
            on_invalid_cursor()
        raise RunError(exc.message) from None
    try:
        store_tokens(engine, source_id, snapshot.tokens, keep_if_same=True)
    except SecretKeyError:
        raise RunError(FETCH_MESSAGE) from None
    if stop():
        if snapshot.cursor_invalidated:
            on_invalid_cursor()
        raise Stopped()
    try:
        stats = _apply_changes(
            engine, radicale, source_id, collection, timezone_name, snapshot, stop
        )
    except (RunError, Stopped):
        if snapshot.cursor_invalidated:
            on_invalid_cursor()
        raise
    if stop():
        if snapshot.cursor_invalidated:
            on_invalid_cursor()
        raise Stopped(stats)
    return ImportResult(stats, snapshot.sync_token, True)


def _apply_changes(
    engine: Engine,
    radicale,
    source_id: str,
    collection: str,
    timezone_name: str,
    snapshot: GoogleSnapshot,
    stop: Callable[[], bool],
) -> RunStats:
    stats = RunStats(seen=len(snapshot.changes))
    live = live_count(radicale, collection, stats)
    present: set[str] = set()
    queued: list[GoogleChange] = []
    blocked = False
    for change in snapshot.changes:
        if stop():
            _count_changes(
                engine,
                radicale,
                source_id,
                collection,
                timezone_name,
                queued,
                stats,
                stop,
            )
            raise Stopped(stats)
        if change.cancelled:
            _count_changes(
                engine,
                radicale,
                source_id,
                collection,
                timezone_name,
                queued,
                stats,
                stop,
            )
            queued = []
            try:
                removed = delete_mapped(
                    engine, radicale, source_id, collection, change.external_id
                )
            except RunError as exc:
                reraise(exc, stats)
            if removed:
                stats.deleted += 1
                live -= 1
            continue
        is_new = mapping_is_new(engine, source_id, change.external_id)
        if is_new and live >= MAX_EVENTS:
            blocked = True
            break
        if is_new:
            live += 1
        present.add(change.external_id)
        queued.append(change)
    _count_changes(
        engine, radicale, source_id, collection, timezone_name, queued, stats, stop
    )
    if blocked:
        raise RunError(CAPACITY_MESSAGE, stats)
    if stop():
        raise Stopped(stats)
    if snapshot.full:
        delete_absent_mapped(
            engine, radicale, source_id, collection, present, stats, stop
        )
    return stats


def _count_changes(
    engine: Engine,
    radicale,
    source_id: str,
    collection: str,
    timezone_name: str,
    changes: list[GoogleChange],
    stats: RunStats,
    stop: Callable[[], bool],
) -> None:
    if not changes:
        return
    try:
        stats.upserted += write_parallel(
            changes,
            lambda change: _upsert_change(
                engine,
                radicale,
                source_id,
                collection,
                timezone_name,
                change,
            )[0],
            stop,
        )
    except (RunError, Stopped) as exc:
        reraise(exc, stats)


def _upsert_change(
    engine: Engine,
    radicale,
    source_id: str,
    collection: str,
    timezone_name: str,
    change: GoogleChange,
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
    if change.replace or not found or deleted is not None:
        obj = _object_from_change(change, ical_uid)
    else:
        obj = _merged_change(radicale, collection, timezone_name, change, ical_uid)
    body = build_object(obj).encode("utf-8")
    path = event_path(collection, ical_uid)
    if found and deleted is None and version == change.etag:
        current = read_event(radicale, path)
        if current == body:
            return False, False
    put_event(radicale, path, body, fresh=is_new)
    when = utc_now()
    with Session(engine) as session, session.begin():
        remember(session, source_id, change.external_id, ical_uid, change.etag, when)
    return True, is_new


def _object_from_change(change: GoogleChange, uid: str) -> CalendarObject:
    if change.master is None:
        raise RunError(SAVE_MESSAGE)
    chosen: list = []
    for mark in change.master.exdates + change.added_exdates:
        if not any(
            mark.value == item.value and mark.all_day == item.all_day for item in chosen
        ):
            chosen.append(mark)
    master = replace(change.master, uid=uid, exdates=tuple(chosen))
    components = [master]
    components.extend(replace(item, uid=uid) for item in change.exceptions)
    return CalendarObject(uid=uid, version=change.etag, components=tuple(components))


def _merged_change(
    radicale,
    collection: str,
    timezone_name: str,
    change: GoogleChange,
    ical_uid: str,
) -> CalendarObject:
    path = event_path(collection, ical_uid)
    current = read_event(radicale, path)
    if not current:
        return _object_from_change(change, ical_uid)
    try:
        parsed = parse_vcalendar(current.decode("utf-8"), timezone_name)
    except (UnicodeError, CalendarParseError):
        raise RunError(SAVE_MESSAGE) from None
    if len(parsed) != 1:
        raise RunError(SAVE_MESSAGE)
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
        raise RunError(SAVE_MESSAGE) from None
