"""The shared calendar builder and reader."""

import pytest

from app.sync.ical import (
    CalendarEvent,
    CalendarParseError,
    build_object,
    build_vcalendar,
    parse_vcalendar,
)


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
        "rrule": None,
        "dtstamp": "20261008T120000Z",
    }
    values.update(overrides)
    return CalendarEvent(**values)


def _unfold(text: str) -> str:
    return text.replace("\r\n ", "")


def test_timed_event_keeps_seconds_and_zone() -> None:
    text = build_vcalendar(
        _event(
            summary="Meet",
            description="a;b,c\\d",
            location="Room 1",
            status="CONFIRMED",
            rrule="FREQ=WEEKLY;BYDAY=MO",
        )
    )
    unfolded = _unfold(text)
    assert "BEGIN:VCALENDAR" in text
    assert text.count("BEGIN:VEVENT") == 1
    assert "DTSTART;TZID=Europe/Amsterdam:20261007T090015" in unfolded
    assert "DTEND;TZID=Europe/Amsterdam:20261007T100015" in unfolded
    assert "SUMMARY:Meet" in unfolded
    assert "DESCRIPTION:a\\;b\\,c\\\\d" in unfolded
    assert "LOCATION:Room 1" in unfolded
    assert "STATUS:CONFIRMED" in unfolded
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in unfolded
    assert "action" not in unfolded.lower()
    assert text.endswith("\r\n")


def test_all_day_uses_date_without_a_zone_shift() -> None:
    text = _unfold(
        build_vcalendar(
            _event(start_value="20261007", end_value=None, all_day=True, timezone=None)
        )
    )
    assert "DTSTART;VALUE=DATE:20261007" in text
    assert "DTEND" not in text
    assert "TZID" not in text
    assert "VALUE=DATE:20261006" not in text
    assert "VALUE=DATE:20261008" not in text


def _calendar(*events: str) -> str:
    body = "\r\n".join(events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n{body}\r\nEND:VCALENDAR\r\n"


def test_parse_keeps_all_day_seconds_zone_and_recurrence() -> None:
    text = _calendar(
        "BEGIN:VEVENT",
        "UID:series-1",
        "DTSTAMP:20261001T080000Z",
        "DTSTART;TZID=Europe/Amsterdam:20261007T090015",
        "DTEND;TZID=Europe/Amsterdam:20261007T100015",
        "SUMMARY:Meet\\, again",
        "DESCRIPTION:line\\none",
        "LOCATION:Room 1",
        "STATUS:CONFIRMED",
        "RRULE:FREQ=WEEKLY;BYDAY=MO",
        "EXDATE;TZID=Europe/Amsterdam:20261014T090015",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:series-1",
        "DTSTAMP:20261002T080000Z",
        "RECURRENCE-ID;TZID=Europe/Amsterdam:20261021T090015",
        "DTSTART;TZID=Europe/Amsterdam:20261021T110015",
        "DTEND;TZID=Europe/Amsterdam:20261021T120015",
        "SUMMARY:Moved",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:day-1",
        "DTSTAMP:20261001T080000Z",
        "DTSTART;VALUE=DATE:20261007",
        "SUMMARY:Away",
        "END:VEVENT",
    )
    objects = parse_vcalendar(text, "Europe/Amsterdam")
    assert [item.uid for item in objects] == ["series-1", "day-1"]
    series = objects[0]
    assert series.version == "20261001T080000Z"
    assert len(series.components) == 2
    master, exception = series.components
    assert master.recurrence_id is None
    assert master.start_value == "20261007T090015"
    assert master.end_value == "20261007T100015"
    assert master.timezone == "Europe/Amsterdam"
    assert master.all_day is False
    assert master.summary == "Meet, again"
    assert master.description == "line\none"
    assert master.rrules == ("FREQ=WEEKLY;BYDAY=MO",)
    assert master.exdates[0].value == "20261014T090015"
    assert exception.recurrence_id is not None
    assert exception.recurrence_id.value == "20261021T090015"
    assert exception.dtstamp == "20261002T080000Z"
    day = objects[1].components[0]
    assert day.all_day is True
    assert day.start_value == "20261007"
    assert day.timezone is None
    written = build_object(series)
    assert written.count("BEGIN:VEVENT") == 2
    assert "RECURRENCE-ID;TZID=Europe/Amsterdam:20261021T090015" in written.replace(
        "\r\n ", ""
    )
    assert "EXDATE;TZID=Europe/Amsterdam:20261014T090015" in written.replace(
        "\r\n ", ""
    )


def test_parse_projects_utc_floating_and_other_zones() -> None:
    text = _calendar(
        "BEGIN:VEVENT",
        "UID:utc-1",
        "DTSTAMP:20261001T080000Z",
        "DTSTART:20261007T070015Z",
        "DTEND:20261007T080015Z",
        "SUMMARY:UTC",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:float-1",
        "DTSTAMP:20261001T080000Z",
        "DTSTART:20261007T0900",
        "DTEND:20261007T1000",
        "SUMMARY:Float",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:ny-1",
        "DTSTAMP:20261001T080000Z",
        "DTSTART;TZID=America/New_York:20261007T090000",
        "DURATION:PT1H",
        "SUMMARY:New York",
        "END:VEVENT",
    )
    objects = {
        item.uid: item.components[0]
        for item in parse_vcalendar(text, "Europe/Amsterdam")
    }
    assert objects["utc-1"].start_value == "20261007T090015"
    assert objects["utc-1"].end_value == "20261007T100015"
    assert objects["utc-1"].timezone == "Europe/Amsterdam"
    assert objects["float-1"].start_value == "20261007T0900"
    assert objects["float-1"].end_value == "20261007T1000"
    assert "T090000" not in objects["float-1"].start_value
    assert objects["ny-1"].start_value == "20261007T150000"
    assert objects["ny-1"].end_value == "20261007T160000"


def test_parse_rejects_a_bad_document_as_a_whole() -> None:
    broken = [
        "not a calendar",
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:x\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
        _calendar(
            "BEGIN:VEVENT",
            "UID:only-exception",
            "RECURRENCE-ID;TZID=Europe/Amsterdam:20261007T090000",
            "DTSTART;TZID=Europe/Amsterdam:20261007T090000",
            "SUMMARY:Moved",
            "END:VEVENT",
        ),
    ]
    for text in broken:
        with pytest.raises(CalendarParseError):
            parse_vcalendar(text, "Europe/Amsterdam")


def test_parse_ignores_alarms_and_reads_a_folded_line() -> None:
    text = _calendar(
        "BEGIN:VEVENT",
        "UID:fold-1",
        "DTSTAMP:20261001T080000Z",
        "DTSTART;VALUE=DATE:20261007",
        "SUMMARY:Hello",
        "  there",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:secret title",
        "END:VALARM",
        "END:VEVENT",
    )
    event = parse_vcalendar(text, "Europe/Amsterdam")[0].components[0]
    assert event.summary == "Hello there"
    assert "secret" not in event.description


def test_long_summary_is_folded_on_octet_boundaries() -> None:
    summary = "A" * 80
    text = build_vcalendar(_event(summary=summary))
    assert "\r\n " in text
    assert f"SUMMARY:{summary}" in _unfold(text)
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
