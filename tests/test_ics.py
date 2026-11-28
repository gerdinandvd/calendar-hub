"""ICS import, the planner, and manual sync."""

import json
import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session
from tests.helpers import (
    HX,
    JSON,
    Feed,
    close_feed_server,
    create_ics_source,
    event_body,
    event_path,
    make_calendar,
    make_event,
    mapping_for,
    put_calls,
    source_mappings,
    source_state,
    start_feed_server,
    trigger_sync,
)

from app.db.models import EventMapping, Source, SourceSecret, SyncState
from app.secrets import decrypt
from app.sync.apply import CAPACITY_MESSAGE, SAVE_MESSAGE
from app.sync.ics import (
    FETCH_MESSAGE,
    MAX_BYTES,
    READ_MESSAGE,
    SIZE_MESSAGE,
    TIMEOUT_MESSAGE,
    TIMEOUT_SECONDS,
)
from app.sync.runner import PROCESS_STOPPED, stop_runs, wait_for
from app.sync.scheduler import due_source_ids

HEADER = "Bearer feed-marker-secret"


def test_limits_are_the_design_defaults() -> None:
    assert TIMEOUT_SECONDS == 30
    assert MAX_BYTES == 20 * 1024 * 1024


def test_import_keeps_fields_and_a_second_run_does_not_rewrite(
    hub_client, fake_radicale
) -> None:
    feed = Feed()
    server, base = start_feed_server(feed)
    client, fake = hub_client, fake_radicale
    try:
        series = [
            "BEGIN:VEVENT",
            "UID:series-1",
            "DTSTAMP:20261001T080000Z",
            "DTSTART;TZID=Europe/Amsterdam:20261007T090015",
            "DTEND;TZID=Europe/Amsterdam:20261007T100015",
            "SUMMARY:Meet",
            "DESCRIPTION:notes",
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
            "BEGIN:VEVENT",
            "UID:utc-1",
            "DTSTAMP:20261001T080000Z",
            "DTSTART:20261007T070015Z",
            "DTEND:20261007T080015Z",
            "SUMMARY:UTC",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "UID:keep-1",
            "DTSTAMP:20261001T080000Z",
            "DTSTART;TZID=Europe/Amsterdam:20261007T0900",
            "DTEND;TZID=Europe/Amsterdam:20261007T1000",
            "SUMMARY:Keep",
            "END:VEVENT",
        ]
        feed.default = make_calendar(*series)
        created = create_ics_source(
            client, f"{base}/feed.ics", ics_authorization_header=HEADER
        )
        assert created["kind_label"] == "External ICS"
        assert created["color"] == "#f9ab00"
        assert created["poll_seconds"] == 900
        assert created["ics_authorization_set"] is True
        assert HEADER not in json.dumps(created)
        local = created["collection_path"] + "local.ics"
        fake.events[local] = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"

        assert created["collection_path"] == f"/owner/ics-{created['id']}/"
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        collection = created["collection_path"]
        mappings = source_mappings(client.app.state.engine, created["id"])
        assert len(mappings) == 4
        series_row = mapping_for(mappings, "series-1")
        assert series_row["ical_uid"] != "series-1"
        assert series_row["external_version"] == "20261001T080000Z"
        series_body = event_body(fake, collection, mappings, "series-1")
        assert series_body.count("BEGIN:VEVENT") == 2
        assert f"UID:{series_row['ical_uid']}" in series_body
        assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in series_body
        assert "EXDATE;TZID=Europe/Amsterdam:20261014T090015" in series_body
        assert "RECURRENCE-ID;TZID=Europe/Amsterdam:20261021T090015" in series_body
        assert "DTSTART;TZID=Europe/Amsterdam:20261007T090015" in series_body
        day = event_body(fake, collection, mappings, "day-1")
        assert "DTSTART;VALUE=DATE:20261007" in day
        assert "TZID" not in day
        utc = event_body(fake, collection, mappings, "utc-1")
        assert "DTSTART;TZID=Europe/Amsterdam:20261007T090015" in utc
        keep = event_body(fake, collection, mappings, "keep-1")
        assert "DTSTART;TZID=Europe/Amsterdam:20261007T0900\r\n" in keep
        assert fake.events[local] == b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        assert feed.requests[0]["authorization"] == HEADER
        kept_at = mapping_for(mappings, "keep-1")["last_synced_at"]
        puts = len(put_calls(fake))

        feed.default = make_calendar(
            "BEGIN:VEVENT",
            "UID:series-1",
            "DTSTAMP:20261001T080000Z",
            "DTSTART;TZID=Europe/Amsterdam:20261007T090015",
            "DTEND;TZID=Europe/Amsterdam:20261007T100015",
            "SUMMARY:Meet again",
            "RRULE:FREQ=WEEKLY;BYDAY=MO",
            "EXDATE;TZID=Europe/Amsterdam:20261014T090015",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "UID:day-1",
            "DTSTAMP:20261001T080000Z",
            "DTSTART;VALUE=DATE:20261007",
            "SUMMARY:Away",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "UID:keep-1",
            "DTSTAMP:20261001T080000Z",
            "DTSTART;TZID=Europe/Amsterdam:20261007T0900",
            "DTEND;TZID=Europe/Amsterdam:20261007T1000",
            "SUMMARY:Keep",
            "END:VEVENT",
        )
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        assert len(put_calls(fake)) == puts + 1
        again = source_mappings(client.app.state.engine, created["id"])
        assert len(again) == 4
        kept = mapping_for(again, "keep-1")
        assert kept["last_synced_at"] == kept_at
        gone = mapping_for(again, "utc-1")
        assert gone["deleted_at"]
        assert event_path(collection, again, "utc-1") not in fake.events
        assert fake.events[local] == b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    finally:
        close_feed_server(server)


def test_not_modified_and_failures_leave_the_collection(
    hub_client, fake_radicale, monkeypatch, caplog
) -> None:
    feed = Feed()
    server, base = start_feed_server(feed)
    client, fake = hub_client, fake_radicale
    try:
        feed.default = make_calendar(*make_event("one"))
        created = create_ics_source(client, f"{base}/feed.ics")
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        before = source_state(client.app.state.engine, created["id"])
        synced = source_mappings(client.app.state.engine, created["id"])[0][
            "last_synced_at"
        ]
        puts = len(put_calls(fake))
        cursor = before["cursor"]
        assert json.loads(cursor)["etag"] == '"feed"'

        feed.status = 304
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        after = source_state(client.app.state.engine, created["id"])
        assert after["status"] == "ok"
        assert after["cursor"] == cursor
        assert after["events_upserted"] == 0
        assert len(put_calls(fake)) == puts
        assert (
            source_mappings(client.app.state.engine, created["id"])[0]["last_synced_at"]
            == synced
        )
        assert feed.requests[-1]["if_none_match"] == '"feed"'

        success_at = after["last_success_at"]
        feed.status = 200
        feed.default = b"this is not a calendar"
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        failed = source_state(client.app.state.engine, created["id"])
        assert failed["last_error_message"] == READ_MESSAGE
        assert failed["last_success_at"] == success_at
        assert failed["cursor"] == cursor
        assert len(put_calls(fake)) == puts
        assert "Traceback" not in failed["last_error_message"]

        feed.default = make_calendar(*make_event("one"))
        feed.content_length = 21 * 1024 * 1024
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        large = source_state(client.app.state.engine, created["id"])
        assert large["last_error_message"] == SIZE_MESSAGE
        assert large["last_success_at"] == success_at
        assert len(fake.events) == 1

        feed.content_length = None
        feed.hold = threading.Event()
        monkeypatch.setenv("HUB_ICS_TIMEOUT", "0.2")
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"], timeout=3) == "error"
        timed = source_state(client.app.state.engine, created["id"])
        assert timed["last_error_message"] == TIMEOUT_MESSAGE
        assert timed["last_success_at"] == success_at
        assert len(fake.events) == 1
        feed.hold.set()
    finally:
        if feed.hold is not None:
            feed.hold.set()
        close_feed_server(server)


def test_a_failed_write_keeps_what_was_saved_and_the_cursor(
    hub_client, fake_radicale
) -> None:
    feed = Feed()
    server, base = start_feed_server(feed)
    client, fake = hub_client, fake_radicale
    try:
        feed.default = make_calendar(*make_event("one"), *make_event("two"))
        created = create_ics_source(client, f"{base}/feed.ics")
        fake.fail_after = 1
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        failed = source_state(client.app.state.engine, created["id"])
        assert failed["last_error_message"] == SAVE_MESSAGE
        assert failed["last_success_at"] is None
        assert failed["cursor"] is None
        assert len(fake.events) == 1
        assert len(source_mappings(client.app.state.engine, created["id"])) == 1

        fake.fail_after = None
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        assert len(fake.events) == 2
        assert source_state(client.app.state.engine, created["id"])["cursor"]
    finally:
        close_feed_server(server)


def test_redirect_scheme_is_rejected_and_same_scheme_is_followed(
    hub_client, fake_radicale, monkeypatch
) -> None:
    feed = Feed()
    server, base = start_feed_server(feed)
    client, fake = hub_client, fake_radicale
    try:
        monkeypatch.setenv("HUB_ICS_TIMEOUT", "0.5")
        feed.redirect_paths["/start"] = "https://127.0.0.1:9/feed.ics"
        created = create_ics_source(client, f"{base}/start")
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        assert (
            source_state(client.app.state.engine, created["id"])["last_error_message"]
            == FETCH_MESSAGE
        )
        assert fake.events == {}
        assert len(feed.requests) == 1

        feed.redirect_paths["/start"] = "/calendar.ics"
        feed.bodies["/calendar.ics"] = make_calendar(*make_event("moved"))
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        moved = source_mappings(client.app.state.engine, created["id"])
        assert any(
            path.endswith(f"{row['ical_uid']}.ics")
            for path in fake.events
            for row in moved
            if row["external_id"] == "moved"
        )
        assert [item["path"] for item in feed.requests[-2:]] == [
            "/start",
            "/calendar.ics",
        ]
    finally:
        close_feed_server(server)


def test_manual_sync_answers_and_the_button(hub_client, fake_radicale) -> None:
    feed = Feed()
    feed.hold = threading.Event()
    server, base = start_feed_server(feed)
    client, _fake = hub_client, fake_radicale
    try:
        feed.default = make_calendar(*make_event("one"))
        created = create_ics_source(client, f"{base}/feed.ics")
        private = client.post("/api/sources", json={"name": "Home"}, headers=JSON)
        assert private.status_code == 201
        hook = client.post(
            "/api/sources", json={"name": "Hook", "kind": "webhook"}, headers=JSON
        )
        assert hook.status_code == 201
        page = client.get("/sources")
        assert page.text.count("Sync now") == 1
        assert "External ICS" in page.text
        assert client.get("/").text.count("Sync now") == 0

        blocked = client.post(f"/sources/{created['id']}/sync?view=sources")
        assert blocked.status_code == 400
        assert source_state(client.app.state.engine, created["id"])["status"] == "idle"

        off = client.patch(
            f"/api/sources/{created['id']}",
            json={"enabled": 0},
            headers=JSON,
        )
        assert off.status_code == 200
        refused = trigger_sync(client, created["id"])
        assert refused.status_code == 422
        assert refused.json()["error"] == "sync_disabled"
        button = client.post(
            f"/sources/{created['id']}/sync?view=sources",
            headers=HX,
        )
        assert "Sync is turned off for this source." in button.text
        assert source_state(client.app.state.engine, created["id"])["status"] == "idle"
        assert feed.requests == []

        client.patch(
            f"/api/sources/{created['id']}",
            json={"enabled": 1},
            headers=JSON,
        )
        first = trigger_sync(client, created["id"])
        assert first.status_code == 202
        assert first.json()["status"] == "running"
        deadline = time.monotonic() + 2
        while len(feed.requests) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        second = trigger_sync(client, created["id"])
        assert second.status_code == 409
        assert second.json()["error"] == "sync_running"
        panel = client.post(f"/sources/{created['id']}/sync?view=sync", headers=HX)
        assert panel.status_code == 200
        assert "A sync is already running." in panel.text
        assert ">running<" in panel.text
        assert len(feed.requests) == 1

        private_sync = trigger_sync(client, private.json()["id"])
        assert private_sync.status_code == 422
        assert private_sync.json()["error"] == "sync_not_supported"
        hook_sync = trigger_sync(client, hook.json()["id"])
        assert hook_sync.status_code == 422
        assert hook_sync.json()["error"] == "sync_not_supported"
        assert (
            source_state(client.app.state.engine, private.json()["id"])["status"]
            == "na"
        )
    finally:
        feed.hold.set()
        wait_for(client.app.state.engine, created["id"])
        close_feed_server(server)


def test_header_stays_out_of_pages_and_logs(hub_client, fake_radicale, caplog) -> None:
    feed = Feed()
    server, base = start_feed_server(feed)
    client = hub_client
    try:
        feed.default = make_calendar(*make_event("one"))
        with caplog.at_level("INFO", logger="app"):
            created = create_ics_source(
                client, f"{base}/feed.ics", ics_authorization_header=HEADER
            )
            assert trigger_sync(client, created["id"]).status_code == 202
            assert wait_for(client.app.state.engine, created["id"]) == "ok"
        page = client.get(f"/sources/{created['id']}")
        assert "An authorization header is set." in page.text
        assert HEADER not in page.text
        assert HEADER not in client.get("/api/sources").text
        assert HEADER not in caplog.text
        with Session(client.app.state.engine) as session:
            stored = session.get(SourceSecret, created["id"]).ics_authorization_header
        assert stored
        assert HEADER not in stored
        assert decrypt(stored) == HEADER
    finally:
        close_feed_server(server)


def test_ics_form_rejects_a_bad_url_and_keeps_the_header_choice(
    hub_client, fake_radicale
) -> None:
    client, fake = hub_client, fake_radicale
    form = client.get("/sources/new?kind=ics")
    assert 'for="ics_url"' in form.text
    assert 'for="poll_seconds"' in form.text
    assert 'for="ics_allow_http"' in form.text
    assert ">External ICS<" in form.text
    bad = client.post(
        "/sources?kind=ics",
        data={
            "name": "Feed",
            "ics_url": "http://example.com/feed.ics",
            "poll_seconds": "900",
        },
        headers=HX,
    )
    assert bad.status_code == 200
    assert bad.text.count("Enter an https URL.") >= 2
    assert fake.calls == []
    bad_poll = client.post(
        "/sources?kind=ics",
        data={
            "name": "Feed",
            "ics_url": "https://example.com/feed.ics",
            "poll_seconds": "soon",
        },
        headers=HX,
    )
    assert "Enter a poll interval in seconds." in bad_poll.text
    assert fake.calls == []


def test_planner_uses_the_clock_and_a_changed_source_is_fetched_again(
    hub_client, fake_radicale
) -> None:
    feed = Feed()
    server, base = start_feed_server(feed)
    client, fake = hub_client, fake_radicale
    try:
        feed.default = make_calendar(
            "BEGIN:VEVENT",
            "UID:ny-1",
            "DTSTAMP:20261001T080000Z",
            "DTSTART;TZID=America/New_York:20261007T090000",
            "DTEND;TZID=America/New_York:20261007T100000",
            "SUMMARY:New York",
            "END:VEVENT",
        )
        created = create_ics_source(client, f"{base}/feed.ics", poll_seconds=3600)
        engine = client.app.state.engine
        moment = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
        with Session(engine) as session, session.begin():
            state = session.get(SyncState, created["id"])
            state.status = "ok"
            state.last_success_at = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert due_source_ids(engine, moment + timedelta(seconds=3599)) == []
        with Session(engine) as session, session.begin():
            session.get(Source, created["id"]).enabled = 0
        assert due_source_ids(engine, moment + timedelta(seconds=3600)) == []
        with Session(engine) as session, session.begin():
            session.get(Source, created["id"]).enabled = 1
        assert due_source_ids(engine, moment + timedelta(seconds=3600)) == [
            created["id"]
        ]
        before = len(client.get("/api/sources").json()["sources"])
        poll = client.get(f"/api/sources/{created['id']}").json()["poll_seconds"]
        client.app.state.scheduler.tick(moment + timedelta(seconds=3600))
        assert wait_for(engine, created["id"]) == "ok"
        assert len(client.get("/api/sources").json()["sources"]) == before
        assert (
            client.get(f"/api/sources/{created['id']}").json()["poll_seconds"] == poll
        )
        body = next(iter(fake.events.values())).decode()
        assert "DTSTART;TZID=Europe/Amsterdam:20261007T150000" in body

        changed = client.patch(
            f"/api/sources/{created['id']}",
            json={"timezone": "America/New_York", "ics_url": f"{base}/other.ics"},
            headers=JSON,
        )
        assert changed.status_code == 200
        assert source_state(engine, created["id"])["cursor"] is None
        feed.bodies["/other.ics"] = feed.default
        requests_before = len(feed.requests)
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(engine, created["id"]) == "ok"
        fresh = feed.requests[requests_before]
        assert fresh["path"] == "/other.ics"
        assert fresh["if_none_match"] is None
        rewritten = next(iter(fake.events.values())).decode()
        assert "DTSTART;TZID=America/New_York:20261007T090000" in rewritten

        with Session(engine) as session, session.begin():
            session.add(
                Source(
                    id="private-due",
                    kind="private",
                    name="Private due",
                    color="#1a73e8",
                    collection_path="/owner/private-due/",
                    enabled=1,
                    poll_seconds=300,
                    timezone="Europe/Amsterdam",
                    ics_allow_http=0,
                    created_at="2026-10-08T12:00:00Z",
                    updated_at="2026-10-08T12:00:00Z",
                )
            )
            session.add(
                SyncState(
                    source_id="private-due",
                    status="na",
                    last_success_at="2020-01-01T00:00:00Z",
                    events_seen=0,
                    events_upserted=0,
                    events_deleted=0,
                )
            )
        assert "private-due" not in due_source_ids(
            engine, moment + timedelta(seconds=3600)
        )
    finally:
        close_feed_server(server)


def test_shutdown_marks_a_blocked_run_and_keeps_events(
    hub_client, fake_radicale
) -> None:
    feed = Feed()
    feed.hold = threading.Event()
    server, base = start_feed_server(feed)
    client, fake = hub_client, fake_radicale
    try:
        feed.default = make_calendar(*make_event("one"))
        created = create_ics_source(client, f"{base}/feed.ics")
        assert trigger_sync(client, created["id"]).status_code == 202
        assert (
            source_state(client.app.state.engine, created["id"])["status"] == "running"
        )
        stop_runs(client.app.state.engine, timeout=0.05)
        stopped = source_state(client.app.state.engine, created["id"])
        assert stopped["status"] == "error"
        assert stopped["last_error_message"] == PROCESS_STOPPED
        assert fake.events == {}
        assert stopped["cursor"] is None
        feed.hold.set()
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        assert fake.events == {}
        assert source_state(client.app.state.engine, created["id"])["cursor"] is None
        assert (
            source_state(client.app.state.engine, created["id"])["last_error_message"]
            == PROCESS_STOPPED
        )
    finally:
        feed.hold.set()
        close_feed_server(server)


def _restamp(lines: list[str], stamp: str) -> list[str]:
    return [
        f"DTSTAMP:{stamp}" if line.startswith("DTSTAMP:") else line for line in lines
    ]


def test_event_limit_keeps_written_events_and_the_cursor(
    hub_client, fake_radicale, monkeypatch
) -> None:
    monkeypatch.setattr("app.sync.apply.MAX_EVENTS", 5)
    feed = Feed()
    server, base = start_feed_server(feed)
    client, fake = hub_client, fake_radicale
    try:
        feed.default = make_calendar(*make_event("one"))
        created = create_ics_source(client, f"{base}/feed.ics")
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        before = source_state(client.app.state.engine, created["id"])
        synced = source_mappings(client.app.state.engine, created["id"])[0][
            "last_synced_at"
        ]
        collection = created["collection_path"]
        for number in range(3):
            fake.events[f"{collection}pad-{number}.ics"] = b"x"
        assert fake.count_events(collection) == 4

        feed.etag = '"next"'
        feed.default = make_calendar(
            *make_event("one"), *make_event("new-a"), *make_event("new-b")
        )
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        failed = source_state(client.app.state.engine, created["id"])
        assert failed["last_error_message"] == CAPACITY_MESSAGE
        assert failed["cursor"] == before["cursor"]
        assert failed["last_success_at"] == before["last_success_at"]
        rows = source_mappings(client.app.state.engine, created["id"])
        assert {row["external_id"] for row in rows} == {"one", "new-a"}
        assert event_path(collection, rows, "new-a") in fake.events
        assert not any(row["external_id"] == "new-b" for row in rows)
        kept = mapping_for(rows, "one")
        assert kept["last_synced_at"] == synced
        assert fake.count_events(collection) == 5

        feed.etag = '"edited"'
        feed.default = make_calendar(
            *_restamp(make_event("one"), "20261002T080000Z"), *make_event("new-a")
        )
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        edited = source_state(client.app.state.engine, created["id"])
        assert edited["cursor"] != before["cursor"]
        rows = source_mappings(client.app.state.engine, created["id"])
        assert (
            b"DTSTAMP:20261002T080000Z"
            in fake.events[event_path(collection, rows, "one")]
        )
        assert fake.count_events(collection) == 5
    finally:
        close_feed_server(server)


def test_feed_uid_with_spaces_gets_a_safe_radicale_uid(
    hub_client, fake_radicale
) -> None:
    feed = Feed()
    server, base = start_feed_server(feed)
    client, fake = hub_client, fake_radicale
    try:
        messy = "Ical2026-10-14T10:00:00ICT3.1 Onderzoeksplanning@rooster.avans.one"
        feed.default = make_calendar(
            "BEGIN:VEVENT",
            f"UID:{messy}",
            "DTSTAMP:20261001T080000Z",
            "DTSTART;TZID=Europe/Amsterdam:20261007T090015",
            "DTEND;TZID=Europe/Amsterdam:20261007T100015",
            "SUMMARY:Lesson",
            "END:VEVENT",
        )
        created = create_ics_source(client, f"{base}/feed.ics")
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        rows = source_mappings(client.app.state.engine, created["id"])
        assert len(rows) == 1
        assert rows[0]["external_id"] == messy
        assert " " not in rows[0]["ical_uid"]
        assert ":" not in rows[0]["ical_uid"]
        body = event_body(fake, created["collection_path"], rows, messy)
        assert f"UID:{rows[0]['ical_uid']}" in body
        assert "SUMMARY:Lesson" in body
    finally:
        close_feed_server(server)


def test_feed_metadata_updates_name_and_timezone(hub_client, fake_radicale) -> None:
    feed = Feed()
    server, base = start_feed_server(feed)
    client, fake = hub_client, fake_radicale
    try:
        feed.default = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
            "X-WR-CALNAME:Avans Rooster\r\n"
            "X-WR-TIMEZONE:Europe/Berlin\r\n"
            + "\r\n".join(make_event("meta-1"))
            + "\r\nEND:VCALENDAR\r\n"
        ).encode()
        created = create_ics_source(client, f"{base}/feed.ics", name="Feed")
        assert created["timezone"] == "Europe/Amsterdam"
        assert trigger_sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        detail = client.get(f"/api/sources/{created['id']}").json()
        assert detail["name"] == "Avans Rooster"
        assert detail["timezone"] == "Europe/Berlin"
        assert (
            "proppatch",
            created["collection_path"],
            "Avans Rooster",
            "#f9ab00",
        ) in fake.calls
        page = client.get(f"/sources/{created['id']}")
        assert page.status_code == 200
        assert "alleen-lezen" in page.text or "read-only" in page.text
    finally:
        close_feed_server(server)


def test_migrate_ics_collection_paths_moves_legacy_collections(
    hub_client, fake_radicale
) -> None:
    client, fake = hub_client, fake_radicale
    created = create_ics_source(client, "https://example.com/legacy.ics")
    legacy = f"/owner/{created['id']}/"
    modern = f"/owner/ics-{created['id']}/"
    with Session(client.app.state.engine) as session, session.begin():
        row = session.get(Source, created["id"])
        assert row is not None
        row.collection_path = legacy
        session.add(
            EventMapping(
                id="map-1",
                source_id=created["id"],
                external_id="old",
                ical_uid="old",
                external_version="1",
                last_synced_at="2026-01-01T00:00:00Z",
                deleted_at=None,
            )
        )
        state = session.get(SyncState, created["id"])
        assert state is not None
        state.cursor = '{"etag":"x"}'
    fake.events[f"{legacy}old.ics"] = b"x"
    from app.services.sources.sync_ops import migrate_ics_collection_paths

    migrate_ics_collection_paths(client.app.state.engine, fake)
    detail = client.get(f"/api/sources/{created['id']}").json()
    assert detail["collection_path"] == modern
    assert ("create", modern, created["name"], "#f9ab00") in fake.calls
    assert ("delete", legacy) in fake.calls
    assert source_mappings(client.app.state.engine, created["id"]) == []
    assert source_state(client.app.state.engine, created["id"])["cursor"] is None
