"""Layer-1 domain tests: types, single rrules, errors, and exports."""

from dataclasses import replace

import pytest

from app.domain import (
    CalendarEvent,
    CalendarParseError,
    DomainError,
    NotFoundError,
    TimeValueError,
    ValidationError,
    build_vcalendar,
    expand_object,
    parse_vcalendar,
)
from app.domain.types import CalendarObject, SourceKind, SyncStatus


def _event(**overrides) -> CalendarEvent:
    values = {
        "uid": "uid-1",
        "summary": "Title",
        "description": "",
        "location": "",
        "status": None,
        "start_value": "20261007T090015",
        "end_value": "20261007T100015",
        "all_day": False,
        "timezone": "Europe/Amsterdam",
        "dtstamp": "20261008T120000Z",
    }
    values.update(overrides)
    return CalendarEvent(**values)


def test_calendar_event_has_single_rrules_field() -> None:
    event = _event(rrules=("FREQ=WEEKLY;BYDAY=MO",))
    assert event.rrules == ("FREQ=WEEKLY;BYDAY=MO",)
    assert "rrule" not in event.__dataclass_fields__


def test_rrules_round_trip_build_and_parse() -> None:
    text = build_vcalendar(_event(rrules=("FREQ=WEEKLY;BYDAY=MO", "FREQ=MONTHLY")))
    objects = parse_vcalendar(text, "Europe/Amsterdam")
    master = objects[0].components[0]
    assert master.rrules == ("FREQ=WEEKLY;BYDAY=MO", "FREQ=MONTHLY")
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in text.replace("\r\n ", "")
    assert "RRULE:FREQ=MONTHLY" in text.replace("\r\n ", "")


def test_rrules_present_empty_by_default() -> None:
    event = _event()
    assert event.rrules == ()


def test_rrules_patch_replace() -> None:
    event = _event(rrules=("FREQ=DAILY",))
    updated = replace(event, rrules=("FREQ=WEEKLY;BYDAY=MO",))
    assert updated.rrules == ("FREQ=WEEKLY;BYDAY=MO",)


def test_rrules_patch_clear() -> None:
    event = _event(rrules=("FREQ=DAILY",))
    cleared = replace(event, rrules=())
    assert cleared.rrules == ()
    text = build_vcalendar(cleared)
    assert "RRULE:" not in text


def test_domain_error_hierarchy() -> None:
    assert issubclass(NotFoundError, DomainError)
    assert issubclass(ValidationError, DomainError)
    assert issubclass(CalendarParseError, DomainError)
    assert issubclass(TimeValueError, DomainError)
    assert issubclass(TimeValueError, ValueError)


def test_enums_expose_kept_kinds() -> None:
    assert SourceKind.PRIVATE == "private"
    assert SourceKind.ICS == "ics"
    assert set(SourceKind) == {SourceKind.PRIVATE, SourceKind.ICS}
    assert SyncStatus.NA == "na"


def test_expand_uses_rrules_only() -> None:
    from datetime import UTC, datetime

    master = _event(
        start_value="20320601T090000",
        end_value="20320601T093000",
        timezone="UTC",
        dtstamp="20320601T000000Z",
        rrules=("FREQ=DAILY;COUNT=2",),
    )
    obj = CalendarObject(uid="uid-1", version="1", components=(master,))
    found = expand_object(
        obj,
        datetime(2032, 6, 1, tzinfo=UTC),
        datetime(2032, 6, 10, tzinfo=UTC),
    )
    assert len(found) == 2


def test_calendar_parse_error_rejects_bad_document() -> None:
    with pytest.raises(CalendarParseError):
        parse_vcalendar("not a calendar", "UTC")


def test_domain_package_exports() -> None:
    import app.domain as domain

    for name in (
        "CalendarEvent",
        "SourceKind",
        "expand_object",
        "build_vcalendar",
        "DomainError",
        "NotFoundError",
        "parse_feed",
        "timezone_ok",
    ):
        assert hasattr(domain, name)
