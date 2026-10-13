"""Google Calendar events.list. Returns a snapshot; the importer writes it."""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from urllib.parse import quote, urlencode

from app.sync.google.oauth import (
    FETCH_MESSAGE,
    READ_MESSAGE,
    GoogleError,
    TokenBundle,
    _timeout,
    _utc,
    api_url,
    json_request,
    refresh_if_needed,
)
from app.sync.ical import (
    CalendarEvent,
    CalendarParseError,
    Mark,
    google_point,
    recurrence_parts,
)

PAGE_SIZE = 250
MAX_PAGES = 400
_UPDATED = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z$")
_STABLE_STAMP = "19700101T000000Z"
_LIST_STATUSES = frozenset({200, 410})


class _Gone(Exception):
    pass


@dataclass(frozen=True)
class GoogleChange:
    external_id: str
    etag: str
    ical_uid: str | None
    cancelled: bool
    replace: bool
    master: CalendarEvent | None
    exceptions: tuple[CalendarEvent, ...]
    added_exdates: tuple[Mark, ...]


@dataclass(frozen=True)
class GoogleSnapshot:
    full: bool
    changes: tuple[GoogleChange, ...]
    sync_token: str
    tokens: TokenBundle
    cursor_invalidated: bool


def fetch_events(
    *,
    calendar_id: str,
    cursor: str | None,
    tokens: TokenBundle,
    source_timezone: str,
    on_invalid_cursor: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> GoogleSnapshot:
    """Read every page, then return one snapshot. The caller writes to Radicale."""

    moment = _utc(now)
    if not calendar_id or any(char in calendar_id for char in "\r\n\t "):
        raise GoogleError(FETCH_MESSAGE)
    current = refresh_if_needed(tokens, moment)
    stored = cursor.strip() if isinstance(cursor, str) else ""
    stored_token = stored or None
    try:
        items, sync_token = _list_all(calendar_id, stored_token, current.access_token)
    except _Gone:
        if on_invalid_cursor is not None:
            on_invalid_cursor()
        try:
            items, sync_token = _list_all(calendar_id, None, current.access_token)
        except GoogleError as exc:
            raise GoogleError(exc.message, invalidate_cursor=True) from None
        return GoogleSnapshot(
            full=True,
            changes=_checked(items, source_timezone, full=True, invalidated=True),
            sync_token=sync_token,
            tokens=current,
            cursor_invalidated=True,
        )
    return GoogleSnapshot(
        full=stored_token is None,
        changes=_checked(
            items, source_timezone, full=stored_token is None, invalidated=False
        ),
        sync_token=sync_token,
        tokens=current,
        cursor_invalidated=False,
    )


def _checked(
    items: list,
    source_timezone: str,
    *,
    full: bool,
    invalidated: bool,
) -> tuple[GoogleChange, ...]:
    try:
        return _changes(items, source_timezone, full=full)
    except CalendarParseError:
        raise GoogleError(READ_MESSAGE, invalidate_cursor=invalidated) from None
    except GoogleError as exc:
        raise GoogleError(exc.message, invalidate_cursor=invalidated) from None


def _changes(
    items: list, source_timezone: str, *, full: bool
) -> tuple[GoogleChange, ...]:
    if not isinstance(items, list):
        raise GoogleError(READ_MESSAGE)
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            raise GoogleError(READ_MESSAGE)
        series = item.get("recurringEventId") or item.get("id")
        if (
            not isinstance(series, str)
            or not series.strip()
            or any(char in series for char in "\r\n")
        ):
            raise GoogleError(READ_MESSAGE)
        key = series.strip()
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(item)
    return tuple(_one(key, groups[key], source_timezone, full=full) for key in order)


def _one(
    series_id: str, items: list[dict], source_timezone: str, *, full: bool
) -> GoogleChange:
    masters = [item for item in items if not item.get("recurringEventId")]
    others = [item for item in items if item.get("recurringEventId")]
    if any(_cancelled(item) for item in masters):
        master = masters[0]
        return GoogleChange(
            external_id=series_id,
            etag=_etag(master),
            ical_uid=_ical(master),
            cancelled=True,
            replace=True,
            master=None,
            exceptions=(),
            added_exdates=(),
        )
    master_item = masters[0] if masters else None
    ical = _ical(master_item) if master_item is not None else None
    etag = _etag(master_item) if master_item is not None else ""
    exceptions: list[CalendarEvent] = []
    exdates: list[Mark] = []
    for item in others:
        found = _ical(item)
        if ical is None:
            ical = found
        if _cancelled(item):
            exdates.append(_original(item, source_timezone))
            if not etag:
                etag = _etag(item)
            continue
        if not etag:
            etag = _etag(item)
        exceptions.append(
            _component(
                item,
                ical or "pending",
                source_timezone,
                _original(item, source_timezone),
                include_recurrence=False,
            )
        )
    master_event = None
    if master_item is not None:
        master_event = _component(
            master_item,
            ical or "pending",
            source_timezone,
            None,
            include_recurrence=True,
        )
        if exdates:
            master_event = replace(
                master_event, exdates=_dedupe(master_event.exdates + tuple(exdates))
            )
    if master_event is None and not exceptions and not exdates:
        raise GoogleError(READ_MESSAGE)
    if (master_event is not None or exceptions) and not etag:
        raise GoogleError(READ_MESSAGE)
    return GoogleChange(
        external_id=series_id,
        etag=etag,
        ical_uid=ical,
        cancelled=False,
        replace=full and master_event is not None,
        master=master_event,
        exceptions=tuple(exceptions),
        added_exdates=() if master_event is not None else tuple(exdates),
    )


def _component(
    item: dict,
    uid: str,
    source_timezone: str,
    recurrence_id: Mark | None,
    *,
    include_recurrence: bool,
) -> CalendarEvent:
    if "start" not in item:
        raise GoogleError(READ_MESSAGE)
    try:
        start = google_point(item.get("start"), source_timezone)
        end = (
            None
            if item.get("end") is None
            else google_point(item.get("end"), source_timezone)
        )
        if end is not None and end.all_day != start.all_day:
            raise CalendarParseError()
        parts = (
            recurrence_parts(item.get("recurrence"), source_timezone)
            if include_recurrence and recurrence_id is None
            else recurrence_parts(None, source_timezone)
        )
    except CalendarParseError:
        raise GoogleError(READ_MESSAGE) from None
    return CalendarEvent(
        uid=uid,
        summary=_text(item.get("summary")),
        description=_text(item.get("description")),
        location=_text(item.get("location")),
        status=_vevent_status(item),
        start_value=start.value,
        end_value=None if end is None else end.value,
        all_day=start.all_day,
        timezone=None if start.all_day else start.timezone,
        rrule=None,
        dtstamp=_dtstamp(item),
        rrules=parts.rules,
        exdates=parts.exdates,
        rdates=parts.rdates,
        recurrence_id=recurrence_id,
    )


def _list_all(
    calendar_id: str,
    sync_token: str | None,
    access_token: str,
) -> tuple[list, str]:
    items: list = []
    page: str | None = None
    seen: set[str] = set()
    deadline = time.monotonic() + _timeout()
    sync_out: str | None = None
    for _page_number in range(MAX_PAGES):
        left = deadline - time.monotonic()
        if left <= 0:
            raise GoogleError(FETCH_MESSAGE)
        status, payload = json_request(
            "GET",
            _events_url(calendar_id, None if page else sync_token, page),
            None,
            FETCH_MESSAGE,
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "User-Agent": "Calendar-Hub",
            },
            timeout=left,
            read_statuses=_LIST_STATUSES,
        )
        if status == 410 and sync_token:
            raise _Gone()
        if status != 200 or not isinstance(payload, dict):
            raise GoogleError(FETCH_MESSAGE)
        batch = payload.get("items", [])
        if batch is None:
            batch = []
        if not isinstance(batch, list):
            raise GoogleError(READ_MESSAGE)
        items.extend(batch)
        next_page = payload.get("nextPageToken")
        if isinstance(next_page, str) and next_page:
            if next_page in seen:
                raise GoogleError(FETCH_MESSAGE)
            seen.add(next_page)
            page = next_page
            continue
        next_sync = payload.get("nextSyncToken")
        if (
            isinstance(next_sync, str)
            and next_sync
            and not any(char in next_sync for char in "\r\n")
        ):
            sync_out = next_sync
        break
    else:
        raise GoogleError(FETCH_MESSAGE)
    if not sync_out:
        raise GoogleError(FETCH_MESSAGE)
    return items, sync_out


def _events_url(
    calendar_id: str, sync_token: str | None, page_token: str | None
) -> str:
    params = {"singleEvents": "false", "maxResults": str(PAGE_SIZE)}
    if page_token:
        params["pageToken"] = page_token
    elif sync_token:
        params["syncToken"] = sync_token
    base = api_url().rstrip("/")
    return f"{base}/calendars/{quote(calendar_id, safe='')}/events?{urlencode(params)}"


def _cancelled(item: dict) -> bool:
    status = item.get("status")
    return isinstance(status, str) and status.strip().lower() == "cancelled"


def _etag(item: dict) -> str:
    raw = item.get("etag")
    if (
        not isinstance(raw, str)
        or not raw.strip()
        or any(char in raw for char in "\r\n")
    ):
        return ""
    return raw


def _ical(item: dict) -> str | None:
    raw = item.get("iCalUID")
    if raw is None:
        return None
    if (
        not isinstance(raw, str)
        or not raw.strip()
        or any(char in raw for char in "\r\n")
    ):
        raise GoogleError(READ_MESSAGE)
    return raw.strip()


def _text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise GoogleError(READ_MESSAGE)
    return value


def _vevent_status(item: dict) -> str | None:
    raw = item.get("status")
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if text == "confirmed":
        return "CONFIRMED"
    if text == "tentative":
        return "TENTATIVE"
    return None


def _original(item: dict, source_timezone: str) -> Mark:
    try:
        return google_point(item.get("originalStartTime"), source_timezone)
    except CalendarParseError:
        raise GoogleError(READ_MESSAGE) from None


def _dtstamp(item: dict) -> str:
    raw = item.get("updated")
    if isinstance(raw, str):
        match = _UPDATED.fullmatch(raw.strip())
        if match:
            year, month, day, hour, minute, second = match.groups()
            return f"{year}{month}{day}T{hour}{minute}{second}Z"
    return _STABLE_STAMP


def _dedupe(marks: tuple[Mark, ...]) -> tuple[Mark, ...]:
    chosen: list[Mark] = []
    for mark in marks:
        if not any(
            mark.value == item.value and mark.all_day == item.all_day for item in chosen
        ):
            chosen.append(mark)
    return tuple(chosen)
