"""The shared calendar builder."""

from app.sync.ical import CalendarEvent, build_vcalendar


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
        _event(summary="Meet", description="a;b,c\\d", location="Room 1", status="CONFIRMED", rrule="FREQ=WEEKLY;BYDAY=MO")
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
    text = _unfold(build_vcalendar(_event(start_value="20261007", end_value=None, all_day=True, timezone=None)))
    assert "DTSTART;VALUE=DATE:20261007" in text
    assert "DTEND" not in text
    assert "TZID" not in text
    assert "VALUE=DATE:20261006" not in text
    assert "VALUE=DATE:20261008" not in text


def test_long_summary_is_folded_on_octet_boundaries() -> None:
    summary = "A" * 80
    text = build_vcalendar(_event(summary=summary))
    assert "\r\n " in text
    assert f"SUMMARY:{summary}" in _unfold(text)
    for line in text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75
