"""Expand recurring series into concrete occurrences for a window."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from app.domain.types import CalendarEvent, CalendarObject, Mark, Occurrence

# Cap expansions per series so a forever RRULE cannot fill memory.
_MAX_OCCURRENCES = 400


def expand_object(
    obj: CalendarObject, window_start: datetime, window_end: datetime
) -> list[Occurrence]:
    if not obj.components:
        return []
    master = obj.components[0]
    exceptions = {
        _mark_key(item.recurrence_id): item
        for item in obj.components[1:]
        if item.recurrence_id is not None
    }
    rules = list(master.rrules)
    if not rules and master.recurrence_id is None:
        if _overlaps_window(master, window_start, window_end):
            return [
                Occurrence(
                    event=master,
                    start=_agent_instant(master.start_value, master.all_day),
                    end=(
                        None
                        if master.end_value is None
                        else _agent_instant(master.end_value, master.all_day)
                    ),
                    all_day=master.all_day,
                    timezone=None if master.all_day else master.timezone,
                    is_occurrence=False,
                )
            ]
        return []

    starts = _occurrence_starts(master, rules, window_start, window_end)
    exkeys = {_mark_key(mark) for mark in master.exdates}
    found: list[Occurrence] = []
    duration = _duration(master)
    for start_moment in starts:
        key = _moment_key(start_moment, master.all_day)
        if key in exkeys:
            continue
        override = exceptions.get(key)
        if override is not None:
            if _overlaps_window(override, window_start, window_end):
                found.append(
                    Occurrence(
                        event=override,
                        start=_agent_instant(override.start_value, override.all_day),
                        end=(
                            None
                            if override.end_value is None
                            else _agent_instant(override.end_value, override.all_day)
                        ),
                        all_day=override.all_day,
                        timezone=None if override.all_day else override.timezone,
                        is_occurrence=True,
                    )
                )
            continue
        end_moment = start_moment + duration if duration is not None else None
        start_value = _compact(start_moment, master.all_day)
        end_value = None if end_moment is None else _compact(end_moment, master.all_day)
        event = replace(
            master,
            start_value=start_value,
            end_value=end_value,
            rrules=(),
            exdates=(),
            rdates=(),
            recurrence_id=Mark(
                value=start_value,
                all_day=master.all_day,
                timezone=None if master.all_day else master.timezone,
            ),
        )
        if _overlaps_window(event, window_start, window_end):
            found.append(
                Occurrence(
                    event=event,
                    start=_agent_instant(start_value, master.all_day),
                    end=None
                    if end_value is None
                    else _agent_instant(end_value, master.all_day),
                    all_day=master.all_day,
                    timezone=None if master.all_day else master.timezone,
                    is_occurrence=True,
                )
            )
        if len(found) >= _MAX_OCCURRENCES:
            break
    return found


def _occurrence_starts(
    master: CalendarEvent,
    rules: list[str],
    window_start: datetime,
    window_end: datetime,
) -> list[datetime | date]:
    dtstart = _to_moment(master.start_value, master.all_day, master.timezone)
    if (
        master.all_day
        and isinstance(dtstart, date)
        and not isinstance(dtstart, datetime)
    ):
        # dateutil emits naive datetimes from a date dtstart; use UTC midnight so
        # window comparisons stay offset-aware.
        dtstart = datetime(dtstart.year, dtstart.month, dtstart.day, tzinfo=UTC)
    if not rules:
        return [dtstart]
    moments: list[datetime | date] = []
    bound_start = _as_datetime(window_start)
    bound_end = _as_datetime(window_end)
    for rule in rules:
        text = rule if rule.upper().startswith("RRULE:") else f"RRULE:{rule}"
        try:
            parsed = rrulestr(text, dtstart=dtstart)
        except (ValueError, TypeError):
            continue
        try:
            for moment in parsed.xafter(bound_start, inc=True):
                aware = _as_datetime(moment)
                if aware > bound_end:
                    break
                moments.append(moment)
                if len(moments) >= _MAX_OCCURRENCES:
                    return moments
        except TypeError:
            continue
    for mark in master.rdates:
        moment = _to_moment(mark.value, mark.all_day, mark.timezone or master.timezone)
        if bound_start <= _as_datetime(moment) <= bound_end:
            moments.append(moment)
    return moments


def _duration(event: CalendarEvent) -> timedelta | None:
    if event.end_value is None:
        return None
    start = _to_moment(event.start_value, event.all_day, event.timezone)
    end = _to_moment(event.end_value, event.all_day, event.timezone)
    return _as_datetime(end) - _as_datetime(start)


def _overlaps_window(
    event: CalendarEvent, window_start: datetime, window_end: datetime
) -> bool:
    start = _as_datetime(_to_moment(event.start_value, event.all_day, event.timezone))
    if event.end_value is None:
        end = start + (timedelta(days=1) if event.all_day else timedelta(hours=1))
    else:
        end = _as_datetime(_to_moment(event.end_value, event.all_day, event.timezone))
    return start < window_end and end > window_start


def _to_moment(value: str, all_day: bool, timezone: str | None) -> datetime | date:
    if all_day:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    second = 0
    if len(value) >= 15:
        second = int(value[13:15])
    local = datetime(
        int(value[0:4]),
        int(value[4:6]),
        int(value[6:8]),
        int(value[9:11]),
        int(value[11:13]),
        second,
    )
    zone = ZoneInfo(timezone or "UTC")
    return local.replace(tzinfo=zone)


def _as_datetime(moment: datetime | date) -> datetime:
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            return moment.replace(tzinfo=UTC)
        return moment
    return datetime(moment.year, moment.month, moment.day, tzinfo=UTC)


def _compact(moment: datetime | date, all_day: bool) -> str:
    if all_day:
        if isinstance(moment, datetime):
            moment = moment.date()
        return f"{moment.year:04}{moment.month:02}{moment.day:02}"
    if isinstance(moment, date) and not isinstance(moment, datetime):
        moment = datetime(moment.year, moment.month, moment.day, tzinfo=UTC)
    return (
        f"{moment.year:04}{moment.month:02}{moment.day:02}"
        f"T{moment.hour:02}{moment.minute:02}{moment.second:02}"
    )


def _agent_instant(value: str, all_day: bool) -> str:
    if all_day:
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"
    base = f"{value[0:4]}-{value[4:6]}-{value[6:8]}T{value[9:11]}:{value[11:13]}"
    if len(value) >= 15:
        return f"{base}:{value[13:15]}"
    return f"{base}:00"


def _mark_key(mark: Mark | None) -> str:
    if mark is None:
        return ""
    return f"{int(mark.all_day)}:{mark.value}"


def _moment_key(moment: datetime | date, all_day: bool) -> str:
    return f"{int(all_day)}:{_compact(moment, all_day)}"
