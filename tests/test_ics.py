"""ICS import, the planner, and manual sync."""

import base64
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import EventMapping, Source, SourceSecret, SyncState
from app.main import create_app
from app.radicale.client import RadicaleError
from app.secrets import decrypt
from app.sync.apply import CAPACITY_MESSAGE, MAX_EVENTS, SAVE_MESSAGE
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

JSON = {"Content-Type": "application/json"}
HX = {"HX-Request": "true"}
SECRET_KEY = base64.urlsafe_b64encode(b"calendar-hub-local-dev-key-32b!!").decode()
HEADER = "Bearer feed-marker-secret"


class FakeRadicale:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.events: dict[str, bytes] = {}
        self.fail_put = False
        self.fail_after: int | None = None
        self._lock = threading.Lock()

    def create_collection(self, path: str, display_name: str, color: str) -> None:
        self.calls.append(("create", path, display_name, color))

    def update_properties(self, path: str, display_name: str, color: str) -> None:
        self.calls.append(("proppatch", path, display_name, color))

    def delete_collection(self, path: str) -> None:
        self.calls.append(("delete", path))
        self.events = {
            key: value for key, value in self.events.items() if not key.startswith(path)
        }

    def put_event(self, path: str, body: bytes, *, fresh: bool = False) -> None:
        with self._lock:
            self.calls.append(("put", path, body))
            puts = [call for call in self.calls if call[0] == "put"]
            if self.fail_put or (
                self.fail_after is not None and len(puts) > self.fail_after
            ):
                raise RadicaleError("The calendar could not be saved.")
            self.events[path] = body

    def delete_event(self, path: str) -> None:
        self.calls.append(("delete-event", path))
        self.events.pop(path, None)

    def get_event(self, path: str) -> bytes | None:
        self.calls.append(("get", path))
        return self.events.get(path)

    def count_events(self, path: str) -> int:
        prefix = path if path.endswith("/") else f"{path}/"
        return sum(
            1
            for key in self.events
            if key.startswith(prefix) and key.lower().endswith(".ics")
        )


class Feed:
    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}
        self.default = b""
        self.requests: list[dict[str, str | None]] = []
        self.etag: str | None = '"feed"'
        self.status = 200
        self.hold: threading.Event | None = None
        self.content_length: int | None = None
        self.redirect_paths: dict[str, str] = {}

    def handler(self) -> type[BaseHTTPRequestHandler]:
        feed = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                feed.requests.append(
                    {
                        "path": self.path,
                        "if_none_match": self.headers.get("If-None-Match"),
                        "authorization": self.headers.get("Authorization"),
                    }
                )
                if feed.hold is not None:
                    feed.hold.wait(5)
                if self.path in feed.redirect_paths:
                    self.send_response(302)
                    self.send_header("Location", feed.redirect_paths[self.path])
                    self.end_headers()
                    return
                if feed.content_length is not None:
                    body = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/calendar")
                    self.send_header("Content-Length", str(feed.content_length))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if feed.status == 304:
                    self.send_response(304)
                    if feed.etag:
                        self.send_header("ETag", feed.etag)
                    self.end_headers()
                    return
                body = feed.bodies.get(self.path, feed.default)
                self.send_response(feed.status)
                self.send_header("Content-Type", "text/calendar")
                self.send_header("Content-Length", str(len(body)))
                if feed.etag:
                    self.send_header("ETag", feed.etag)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


def _server(feed: Feed) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), feed.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def _close_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def _open(tmp_path: Path, monkeypatch) -> tuple[TestClient, FakeRadicale]:
    monkeypatch.setenv("HUB_SQLITE_PATH", str(tmp_path / "hub.sqlite"))
    monkeypatch.setenv("HUB_HOST", "localhost:8443")
    monkeypatch.setenv("HUB_USER", "owner")
    monkeypatch.setenv("HUB_PASSWORD", "secret-password-value")
    monkeypatch.setenv("HUB_INTERNAL_USER", "internal-account-name")
    monkeypatch.setenv("HUB_INTERNAL_PASSWORD", "internal-secret-value")
    monkeypatch.setenv("HUB_SECRET_KEY", SECRET_KEY)
    client = TestClient(create_app())
    client.__enter__()
    client.app.state.scheduler.pause()
    fake = FakeRadicale()
    client.app.state.radicale = fake
    client.app.state.scheduler.radicale = fake
    return client, fake


def _close(client: TestClient) -> None:
    client.__exit__(None, None, None)


def _calendar(*events: str) -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", *events, "END:VCALENDAR"]
    return ("\r\n".join(lines) + "\r\n").encode()


def _event(uid: str, **lines: str) -> list[str]:
    body = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        "DTSTAMP:20261001T080000Z",
        "DTSTART;TZID=Europe/Amsterdam:20261007T090015",
        "DTEND;TZID=Europe/Amsterdam:20261007T100015",
        f"SUMMARY:{uid}",
    ]
    body.extend(f"{name}:{value}" for name, value in lines.items())
    body.append("END:VEVENT")
    return body


def _create(client: TestClient, url: str, **extra) -> dict:
    body = {
        "name": extra.pop("name", "Feed"),
        "kind": "ics",
        "ics_url": url,
        "ics_allow_http": extra.pop("ics_allow_http", 1),
        **extra,
    }
    response = client.post("/api/sources", json=body, headers=JSON)
    assert response.status_code == 201, response.text
    return response.json()


def _sync(client: TestClient, source_id: str):
    return client.post(f"/api/sources/{source_id}/sync", json={}, headers=JSON)


def _mappings(engine, source_id: str) -> list[dict]:
    with Session(engine) as session:
        rows = (
            session.query(EventMapping)
            .filter(EventMapping.source_id == source_id)
            .all()
        )
        return [
            {
                "external_id": row.external_id,
                "ical_uid": row.ical_uid,
                "external_version": row.external_version,
                "last_synced_at": row.last_synced_at,
                "deleted_at": row.deleted_at,
            }
            for row in rows
        ]


def _state(engine, source_id: str) -> dict:
    with Session(engine) as session:
        row = session.get(SyncState, source_id)
        assert row is not None
        return {
            "status": row.status,
            "cursor": row.cursor,
            "last_success_at": row.last_success_at,
            "last_error_at": row.last_error_at,
            "last_error_message": row.last_error_message,
            "events_seen": row.events_seen,
            "events_upserted": row.events_upserted,
            "events_deleted": row.events_deleted,
        }


def _puts(fake: FakeRadicale) -> list[tuple]:
    return [call for call in fake.calls if call[0] == "put"]


def test_limits_are_the_design_defaults() -> None:
    assert TIMEOUT_SECONDS == 30
    assert MAX_BYTES == 20 * 1024 * 1024


def test_import_keeps_fields_and_a_second_run_does_not_rewrite(
    tmp_path: Path, monkeypatch
) -> None:
    feed = Feed()
    server, base = _server(feed)
    client, fake = _open(tmp_path, monkeypatch)
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
        feed.default = _calendar(*series)
        created = _create(client, f"{base}/feed.ics", ics_authorization_header=HEADER)
        assert created["kind_label"] == "External ICS"
        assert created["color"] == "#f9ab00"
        assert created["poll_seconds"] == 900
        assert created["ics_authorization_set"] is True
        assert HEADER not in json.dumps(created)
        local = created["collection_path"] + "local.ics"
        fake.events[local] = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"

        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        stored = {path: body.decode() for path, body in fake.events.items()}
        series_body = next(
            body for path, body in stored.items() if path.endswith("series-1.ics")
        )
        assert series_body.count("BEGIN:VEVENT") == 2
        assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in series_body
        assert "EXDATE;TZID=Europe/Amsterdam:20261014T090015" in series_body
        assert "RECURRENCE-ID;TZID=Europe/Amsterdam:20261021T090015" in series_body
        assert "DTSTART;TZID=Europe/Amsterdam:20261007T090015" in series_body
        day = next(body for path, body in stored.items() if path.endswith("day-1.ics"))
        assert "DTSTART;VALUE=DATE:20261007" in day
        assert "TZID" not in day
        utc = next(body for path, body in stored.items() if path.endswith("utc-1.ics"))
        assert "DTSTART;TZID=Europe/Amsterdam:20261007T090015" in utc
        keep = next(
            body for path, body in stored.items() if path.endswith("keep-1.ics")
        )
        assert "DTSTART;TZID=Europe/Amsterdam:20261007T0900\r\n" in keep
        mappings = _mappings(client.app.state.engine, created["id"])
        assert len(mappings) == 4
        series_row = next(row for row in mappings if row["external_id"] == "series-1")
        assert series_row["ical_uid"] == "series-1"
        assert series_row["external_version"] == "20261001T080000Z"
        assert fake.events[local] == b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        assert feed.requests[0]["authorization"] == HEADER
        kept_at = next(
            row["last_synced_at"] for row in mappings if row["external_id"] == "keep-1"
        )
        puts = len(_puts(fake))

        feed.default = _calendar(
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
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        assert len(_puts(fake)) == puts + 1
        again = _mappings(client.app.state.engine, created["id"])
        assert len(again) == 4
        kept = next(row for row in again if row["external_id"] == "keep-1")
        assert kept["last_synced_at"] == kept_at
        gone = next(row for row in again if row["external_id"] == "utc-1")
        assert gone["deleted_at"]
        assert not any(path.endswith("utc-1.ics") for path in fake.events)
        assert fake.events[local] == b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    finally:
        _close(client)
        _close_server(server)


def test_not_modified_and_failures_leave_the_collection(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    feed = Feed()
    server, base = _server(feed)
    client, fake = _open(tmp_path, monkeypatch)
    try:
        feed.default = _calendar(*_event("one"))
        created = _create(client, f"{base}/feed.ics")
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        before = _state(client.app.state.engine, created["id"])
        synced = _mappings(client.app.state.engine, created["id"])[0]["last_synced_at"]
        puts = len(_puts(fake))
        cursor = before["cursor"]
        assert json.loads(cursor)["etag"] == '"feed"'

        feed.status = 304
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        after = _state(client.app.state.engine, created["id"])
        assert after["status"] == "ok"
        assert after["cursor"] == cursor
        assert after["events_upserted"] == 0
        assert len(_puts(fake)) == puts
        assert (
            _mappings(client.app.state.engine, created["id"])[0]["last_synced_at"]
            == synced
        )
        assert feed.requests[-1]["if_none_match"] == '"feed"'

        feed.status = 200
        feed.default = b"this is not a calendar"
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        failed = _state(client.app.state.engine, created["id"])
        assert failed["last_error_message"] == READ_MESSAGE
        assert failed["last_success_at"] == before["last_success_at"]
        assert failed["cursor"] == cursor
        assert len(_puts(fake)) == puts
        assert "Traceback" not in failed["last_error_message"]

        feed.default = _calendar(*_event("one"))
        feed.content_length = 21 * 1024 * 1024
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        large = _state(client.app.state.engine, created["id"])
        assert large["last_error_message"] == SIZE_MESSAGE
        assert large["last_success_at"] == before["last_success_at"]
        assert len(fake.events) == 1

        feed.content_length = None
        feed.hold = threading.Event()
        monkeypatch.setenv("HUB_ICS_TIMEOUT", "0.2")
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"], timeout=3) == "error"
        timed = _state(client.app.state.engine, created["id"])
        assert timed["last_error_message"] == TIMEOUT_MESSAGE
        assert timed["last_success_at"] == before["last_success_at"]
        assert len(fake.events) == 1
        feed.hold.set()
    finally:
        if feed.hold is not None:
            feed.hold.set()
        _close(client)
        _close_server(server)


def test_a_failed_write_keeps_what_was_saved_and_the_cursor(
    tmp_path: Path, monkeypatch
) -> None:
    feed = Feed()
    server, base = _server(feed)
    client, fake = _open(tmp_path, monkeypatch)
    try:
        feed.default = _calendar(*_event("one"), *_event("two"))
        created = _create(client, f"{base}/feed.ics")
        fake.fail_after = 1
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        failed = _state(client.app.state.engine, created["id"])
        assert failed["last_error_message"] == SAVE_MESSAGE
        assert failed["last_success_at"] is None
        assert failed["cursor"] is None
        assert len(fake.events) == 1
        assert len(_mappings(client.app.state.engine, created["id"])) == 1

        fake.fail_after = None
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        assert len(fake.events) == 2
        assert _state(client.app.state.engine, created["id"])["cursor"]
    finally:
        _close(client)
        _close_server(server)


def test_redirect_scheme_is_rejected_and_same_scheme_is_followed(
    tmp_path: Path, monkeypatch
) -> None:
    feed = Feed()
    server, base = _server(feed)
    client, fake = _open(tmp_path, monkeypatch)
    try:
        monkeypatch.setenv("HUB_ICS_TIMEOUT", "0.5")
        feed.redirect_paths["/start"] = "https://127.0.0.1:9/feed.ics"
        created = _create(client, f"{base}/start")
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        assert (
            _state(client.app.state.engine, created["id"])["last_error_message"]
            == FETCH_MESSAGE
        )
        assert fake.events == {}
        assert len(feed.requests) == 1

        feed.redirect_paths["/start"] = "/calendar.ics"
        feed.bodies["/calendar.ics"] = _calendar(*_event("moved"))
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        assert any(path.endswith("moved.ics") for path in fake.events)
        assert [item["path"] for item in feed.requests[-2:]] == [
            "/start",
            "/calendar.ics",
        ]
    finally:
        _close(client)
        _close_server(server)


def test_manual_sync_answers_and_the_button(tmp_path: Path, monkeypatch) -> None:
    feed = Feed()
    feed.hold = threading.Event()
    server, base = _server(feed)
    client, fake = _open(tmp_path, monkeypatch)
    try:
        feed.default = _calendar(*_event("one"))
        created = _create(client, f"{base}/feed.ics")
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
        assert _state(client.app.state.engine, created["id"])["status"] == "idle"

        off = client.patch(
            f"/api/sources/{created['id']}",
            json={"enabled": 0},
            headers=JSON,
        )
        assert off.status_code == 200
        refused = _sync(client, created["id"])
        assert refused.status_code == 422
        assert refused.json()["error"] == "sync_disabled"
        button = client.post(
            f"/sources/{created['id']}/sync?view=sources",
            headers=HX,
        )
        assert "Sync is turned off for this source." in button.text
        assert _state(client.app.state.engine, created["id"])["status"] == "idle"
        assert feed.requests == []

        client.patch(
            f"/api/sources/{created['id']}",
            json={"enabled": 1},
            headers=JSON,
        )
        first = _sync(client, created["id"])
        assert first.status_code == 202
        assert first.json()["status"] == "running"
        deadline = time.monotonic() + 2
        while len(feed.requests) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        second = _sync(client, created["id"])
        assert second.status_code == 409
        assert second.json()["error"] == "sync_running"
        panel = client.post(f"/sources/{created['id']}/sync?view=sync", headers=HX)
        assert panel.status_code == 200
        assert "A sync is already running." in panel.text
        assert ">running<" in panel.text
        assert len(feed.requests) == 1

        private_sync = _sync(client, private.json()["id"])
        assert private_sync.status_code == 422
        assert private_sync.json()["error"] == "sync_not_supported"
        hook_sync = _sync(client, hook.json()["id"])
        assert hook_sync.status_code == 422
        assert hook_sync.json()["error"] == "sync_not_supported"
        assert _state(client.app.state.engine, private.json()["id"])["status"] == "na"
    finally:
        feed.hold.set()
        wait_for(client.app.state.engine, created["id"])
        _close(client)
        _close_server(server)


def test_header_stays_out_of_pages_and_logs(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    feed = Feed()
    server, base = _server(feed)
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        feed.default = _calendar(*_event("one"))
        with caplog.at_level("INFO", logger="app"):
            created = _create(
                client, f"{base}/feed.ics", ics_authorization_header=HEADER
            )
            assert _sync(client, created["id"]).status_code == 202
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
        _close(client)
        _close_server(server)


def test_ics_form_rejects_a_bad_url_and_keeps_the_header_choice(
    tmp_path: Path, monkeypatch
) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
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
    finally:
        _close(client)


def test_planner_uses_the_clock_and_a_changed_source_is_fetched_again(
    tmp_path: Path, monkeypatch
) -> None:
    feed = Feed()
    server, base = _server(feed)
    client, fake = _open(tmp_path, monkeypatch)
    try:
        feed.default = _calendar(
            "BEGIN:VEVENT",
            "UID:ny-1",
            "DTSTAMP:20261001T080000Z",
            "DTSTART;TZID=America/New_York:20261007T090000",
            "DTEND;TZID=America/New_York:20261007T100000",
            "SUMMARY:New York",
            "END:VEVENT",
        )
        created = _create(client, f"{base}/feed.ics", poll_seconds=3600)
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
        assert _state(engine, created["id"])["cursor"] is None
        feed.bodies["/other.ics"] = feed.default
        requests_before = len(feed.requests)
        assert _sync(client, created["id"]).status_code == 202
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
        _close(client)
        _close_server(server)


def test_shutdown_marks_a_blocked_run_and_keeps_events(
    tmp_path: Path, monkeypatch
) -> None:
    feed = Feed()
    feed.hold = threading.Event()
    server, base = _server(feed)
    client, fake = _open(tmp_path, monkeypatch)
    try:
        feed.default = _calendar(*_event("one"))
        created = _create(client, f"{base}/feed.ics")
        assert _sync(client, created["id"]).status_code == 202
        assert _state(client.app.state.engine, created["id"])["status"] == "running"
        stop_runs(client.app.state.engine, timeout=0.05)
        stopped = _state(client.app.state.engine, created["id"])
        assert stopped["status"] == "error"
        assert stopped["last_error_message"] == PROCESS_STOPPED
        assert fake.events == {}
        assert stopped["cursor"] is None
        feed.hold.set()
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        assert fake.events == {}
        assert _state(client.app.state.engine, created["id"])["cursor"] is None
        assert (
            _state(client.app.state.engine, created["id"])["last_error_message"]
            == PROCESS_STOPPED
        )
    finally:
        feed.hold.set()
        _close(client)
        _close_server(server)


def _restamp(lines: list[str], stamp: str) -> list[str]:
    return [
        f"DTSTAMP:{stamp}" if line.startswith("DTSTAMP:") else line for line in lines
    ]


def test_event_limit_keeps_written_events_and_the_cursor(
    tmp_path: Path, monkeypatch
) -> None:
    feed = Feed()
    server, base = _server(feed)
    client, fake = _open(tmp_path, monkeypatch)
    try:
        feed.default = _calendar(*_event("one"))
        created = _create(client, f"{base}/feed.ics")
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        before = _state(client.app.state.engine, created["id"])
        synced = _mappings(client.app.state.engine, created["id"])[0]["last_synced_at"]
        collection = created["collection_path"]
        for number in range(MAX_EVENTS - 2):
            fake.events[f"{collection}pad-{number}.ics"] = b"x"
        assert fake.count_events(collection) == MAX_EVENTS - 1

        feed.etag = '"next"'
        feed.default = _calendar(*_event("one"), *_event("new-a"), *_event("new-b"))
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        failed = _state(client.app.state.engine, created["id"])
        assert failed["last_error_message"] == CAPACITY_MESSAGE
        assert failed["cursor"] == before["cursor"]
        assert failed["last_success_at"] == before["last_success_at"]
        assert any(path.endswith("/new-a.ics") for path in fake.events)
        assert not any(path.endswith("/new-b.ics") for path in fake.events)
        rows = _mappings(client.app.state.engine, created["id"])
        assert {row["external_id"] for row in rows} == {"one", "new-a"}
        kept = next(row for row in rows if row["external_id"] == "one")
        assert kept["last_synced_at"] == synced
        assert fake.count_events(collection) == MAX_EVENTS

        feed.etag = '"edited"'
        feed.default = _calendar(
            *_restamp(_event("one"), "20261002T080000Z"), *_event("new-a")
        )
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        edited = _state(client.app.state.engine, created["id"])
        assert edited["cursor"] != before["cursor"]
        assert b"DTSTAMP:20261002T080000Z" in fake.events[f"{collection}one.ics"]
        assert fake.count_events(collection) == MAX_EVENTS
    finally:
        _close(client)
        _close_server(server)
