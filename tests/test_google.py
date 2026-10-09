"""Google read-only import against a stand-in calendar."""

import base64
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import EventMapping, SourceSecret, SyncState
from app.main import create_app
from app.radicale.client import RadicaleError
from app.secrets import decrypt, encrypt
from app.sync.google import READONLY_SCOPE, _readonly_scope
from app.sync.runner import wait_for
from app.sync.scheduler import due_source_ids

JSON = {"Content-Type": "application/json"}
HX = {"HX-Request": "true"}
SECRET_KEY = base64.urlsafe_b64encode(b"calendar-hub-local-dev-key-32b!!").decode()

WRITE_SCOPE = "https://www.googleapis.com/auth/calendar"


class FakeRadicale:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.events: dict[str, bytes] = {}
        self.fail_after: int | None = None

    def create_collection(self, path: str, display_name: str, color: str) -> None:
        self.calls.append(("create", path, display_name, color))

    def update_properties(self, path: str, display_name: str, color: str) -> None:
        self.calls.append(("proppatch", path, display_name, color))

    def delete_collection(self, path: str) -> None:
        self.calls.append(("delete", path))
        self.events = {key: value for key, value in self.events.items() if not key.startswith(path)}

    def put_event(self, path: str, body: bytes) -> None:
        self.calls.append(("put", path, body))
        puts = [call for call in self.calls if call[0] == "put"]
        if self.fail_after is not None and len(puts) > self.fail_after:
            raise RadicaleError("The calendar could not be saved.")
        self.events[path] = body

    def delete_event(self, path: str) -> None:
        self.calls.append(("delete-event", path))
        self.events.pop(path, None)

    def get_event(self, path: str) -> bytes | None:
        self.calls.append(("get", path))
        return self.events.get(path)


class FakeGoogle:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.token_status = 200
        self.token_payload: dict = {
            "access_token": "access-marker-value",
            "refresh_token": "refresh-marker-value",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": READONLY_SCOPE,
        }
        self.events: list[tuple[int, dict]] = []

    def push(self, status: int, payload: dict) -> None:
        self.events.append((status, payload))

    def handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._take("GET")

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length:
                    self.rfile.read(length)
                self._take("POST")

            def do_PUT(self) -> None:
                self._reject("PUT")

            def do_PATCH(self) -> None:
                self._reject("PATCH")

            def do_DELETE(self) -> None:
                self._reject("DELETE")

            def _reject(self, method: str) -> None:
                fake.requests.append({"method": method, "path": urlsplit(self.path).path, "params": {}})
                self.send_response(405)
                self.end_headers()

            def _take(self, method: str) -> None:
                parsed = urlsplit(self.path)
                fake.requests.append(
                    {
                        "method": method,
                        "path": parsed.path,
                        "params": {key: values[-1] for key, values in parse_qs(parsed.query).items()},
                    }
                )
                if parsed.path == "/token":
                    self._send(fake.token_status, fake.token_payload)
                    return
                if not fake.events:
                    self._send(500, {"error": "empty"})
                    return
                status, payload = fake.events.pop(0)
                self._send(status, payload)

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        return Handler


def _server(fake: FakeGoogle) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def _close_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def _open(tmp_path: Path, monkeypatch, base: str) -> tuple[TestClient, FakeRadicale]:
    monkeypatch.setenv("HUB_SQLITE_PATH", str(tmp_path / "hub.sqlite"))
    monkeypatch.setenv("HUB_HOST", "localhost:8443")
    monkeypatch.setenv("HUB_USER", "owner")
    monkeypatch.setenv("HUB_PASSWORD", "secret-password-value")
    monkeypatch.setenv("HUB_INTERNAL_USER", "internal-account-name")
    monkeypatch.setenv("HUB_INTERNAL_PASSWORD", "internal-secret-value")
    monkeypatch.setenv("HUB_SECRET_KEY", SECRET_KEY)
    monkeypatch.setenv("HUB_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("HUB_GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("HUB_GOOGLE_AUTH_URL", f"{base}/auth")
    monkeypatch.setenv("HUB_GOOGLE_TOKEN_URL", f"{base}/token")
    monkeypatch.setenv("HUB_GOOGLE_API_URL", base)
    client = TestClient(create_app())
    client.__enter__()
    client.app.state.scheduler.pause()
    fake = FakeRadicale()
    client.app.state.radicale = fake
    client.app.state.scheduler.radicale = fake
    return client, fake


def _close(client: TestClient) -> None:
    client.__exit__(None, None, None)


def _create(client: TestClient, name: str = "Work", **extra: object) -> dict:
    body = {"name": name, "kind": "google", **extra}
    response = client.post("/api/sources", json=body, headers=JSON)
    assert response.status_code == 201, response.text
    return response.json()


def _start(client: TestClient, source_id: str):
    return client.get(f"/api/oauth/google/start?source_id={source_id}", follow_redirects=False)


def _callback(client: TestClient, state: str | None, code: str | None = "code-1"):
    params = {}
    if state is not None:
        params["state"] = state
    if code is not None:
        params["code"] = code
    return client.get("/api/oauth/google/callback", params=params, follow_redirects=False)


def _connect(client: TestClient, source_id: str) -> str:
    started = _start(client, source_id)
    assert started.status_code == 302
    state = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    done = _callback(client, state)
    assert done.status_code == 302
    assert urlsplit(done.headers["location"]).path == f"/sources/{source_id}"
    return state


def _sync(client: TestClient, source_id: str):
    return client.post(f"/api/sources/{source_id}/sync", headers=JSON)


def _state(engine, source_id: str) -> dict:
    with Session(engine) as session:
        row = session.get(SyncState, source_id)
        return {
            "status": row.status,
            "cursor": row.cursor,
            "last_success_at": row.last_success_at,
            "last_error_message": row.last_error_message,
            "events_seen": row.events_seen,
            "events_upserted": row.events_upserted,
            "events_deleted": row.events_deleted,
        }


def _mappings(engine, source_id: str) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(select(EventMapping).where(EventMapping.source_id == source_id)).all()
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


def _event_body(fake: FakeRadicale, uid: str) -> str:
    return next(body.decode() for body in fake.events.values() if f"UID:{uid}" in body.decode())


def _item(event_id: str, etag: str, **extra: object) -> dict:
    item = {
        "id": event_id,
        "etag": etag,
        "status": "confirmed",
        "summary": event_id,
        "iCalUID": f"{event_id}@google.com",
        "updated": "2026-10-01T08:00:00Z",
        "start": {"dateTime": "2026-10-07T09:00:30+02:00", "timeZone": "Europe/Amsterdam"},
        "end": {"dateTime": "2026-10-07T10:00:30+02:00", "timeZone": "Europe/Amsterdam"},
    }
    item.update(extra)
    return item


def _page(items: list[dict], token: str) -> tuple[int, dict]:
    return 200, {"items": items, "nextSyncToken": token}


def _calendar_calls(fake: FakeGoogle) -> list[dict]:
    return [item for item in fake.requests if item["path"].startswith("/calendars/")]


def test_readonly_scope_accepts_only_the_read_scope() -> None:
    assert _readonly_scope(None)
    assert _readonly_scope("  ")
    assert _readonly_scope(READONLY_SCOPE)
    assert not _readonly_scope(WRITE_SCOPE)
    assert not _readonly_scope(f"{READONLY_SCOPE} {WRITE_SCOPE}")


def test_oauth_state_is_one_time_and_the_button_says_read_only(tmp_path: Path, monkeypatch, caplog) -> None:
    google = FakeGoogle()
    server, base = _server(google)
    client, fake = _open(tmp_path, monkeypatch, base)
    try:
        created = _create(client)
        page = client.get(f"/sources/{created['id']}")
        link = (
            f'<a href="/api/oauth/google/start?source_id={created["id"]}">'
            "Connect. This connection only reads and does not write back.</a>"
        )
        assert link in page.text
        assert "hx-get" not in link

        started = _start(client, created["id"])
        query = parse_qs(urlsplit(started.headers["location"]).query)
        assert query["scope"] == [READONLY_SCOPE]
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]
        assert query["response_type"] == ["code"]
        assert "include_granted_scopes" not in query
        assert query["redirect_uri"] == ["https://localhost:8443/api/oauth/google/callback"]
        state = query["state"][0]
        assert _calendar_calls(google) == []
        assert google.requests == []

        missing = _callback(client, None)
        assert urlsplit(missing.headers["location"]).path == "/"
        assert parse_qs(urlsplit(missing.headers["location"]).query)["google"] == ["state"]
        assert google.requests == []

        foreign = _callback(client, "not-from-the-hub")
        assert parse_qs(urlsplit(foreign.headers["location"]).query)["google"] == ["state"]
        assert google.requests == []

        google.token_payload = {**google.token_payload, "scope": WRITE_SCOPE}
        rejected = _callback(client, state)
        assert parse_qs(urlsplit(rejected.headers["location"]).query)["google"] == ["scope"]
        assert [item["path"] for item in google.requests] == ["/token"]
        home = client.get("/?google=scope")
        assert "The Google connection can write, so it was not saved." in home.text
        with Session(client.app.state.engine) as session:
            assert session.get(SourceSecret, created["id"]) is None

        google.token_payload = {**google.token_payload, "scope": READONLY_SCOPE}
        with caplog.at_level("INFO", logger="app"):
            again = _start(client, created["id"])
            fresh = parse_qs(urlsplit(again.headers["location"]).query)["state"][0]
            done = _callback(client, fresh)
        assert urlsplit(done.headers["location"]).path == f"/sources/{created['id']}"
        with Session(client.app.state.engine) as session:
            secret = session.get(SourceSecret, created["id"])
            stored_refresh = secret.oauth_refresh_token
            stored_access = secret.oauth_access_token
        assert decrypt(stored_refresh) == "refresh-marker-value"
        assert decrypt(stored_access) == "access-marker-value"
        assert "refresh-marker-value" not in stored_refresh
        reused = _callback(client, fresh)
        assert parse_qs(urlsplit(reused.headers["location"]).query)["google"] == ["state"]
        with Session(client.app.state.engine) as session:
            assert session.get(SourceSecret, created["id"]).oauth_refresh_token == stored_refresh
        connected = client.get(f"/sources/{created['id']}")
        assert "Reconnect. This connection only reads and does not write back." in connected.text
        assert "refresh-marker-value" not in connected.text
        assert "access-marker-value" not in connected.text
        assert "refresh-marker-value" not in client.get("/api/sources").text
        assert "refresh-marker-value" not in caplog.text
        assert "access-marker-value" not in caplog.text
        assert "Weekly moved" not in caplog.text
        assert fake.calls == [("create", created["collection_path"], "Work", "#188038")]

        google.token_payload = {
            **google.token_payload,
            "scope": READONLY_SCOPE,
            "refresh_token": "refresh-again",
            "access_token": "access-again",
        }
        replaced = _start(client, created["id"])
        replaced_state = parse_qs(urlsplit(replaced.headers["location"]).query)["state"][0]
        assert _callback(client, replaced_state).status_code == 302
        with Session(client.app.state.engine) as session:
            secret = session.get(SourceSecret, created["id"])
            assert decrypt(secret.oauth_refresh_token) == "refresh-again"
            assert decrypt(secret.oauth_access_token) == "access-again"
        google.token_payload = {
            **google.token_payload,
            "scope": WRITE_SCOPE,
            "refresh_token": "refresh-write",
            "access_token": "access-write",
        }
        blocked = _start(client, created["id"])
        blocked_state = parse_qs(urlsplit(blocked.headers["location"]).query)["state"][0]
        refused = _callback(client, blocked_state)
        assert parse_qs(urlsplit(refused.headers["location"]).query)["google"] == ["scope"]
        with Session(client.app.state.engine) as session:
            secret = session.get(SourceSecret, created["id"])
            assert decrypt(secret.oauth_refresh_token) == "refresh-again"
            assert decrypt(secret.oauth_access_token) == "access-again"
    finally:
        _close(client)
        _close_server(server)


def test_google_form_uses_primary_and_a_300_second_poll(tmp_path: Path, monkeypatch) -> None:
    google = FakeGoogle()
    server, base = _server(google)
    client, fake = _open(tmp_path, monkeypatch, base)
    try:
        form = client.get("/sources/new?kind=google")
        assert 'for="google_calendar_id"' in form.text
        assert 'for="poll_seconds"' in form.text
        assert ">Google<" in form.text
        assert 'value="primary"' in form.text
        assert 'value="300"' in form.text
        bad = client.post(
            "/sources?kind=google",
            data={"name": "Work", "google_calendar_id": "my id", "poll_seconds": "300"},
            headers=HX,
        )
        assert bad.text.count("Enter a calendar id.") >= 2
        assert fake.calls == []
        created = _create(client, "Desk")
        assert created["google_calendar_id"] == "primary"
        assert created["poll_seconds"] == 300
        assert created["google_connected"] is False
        assert "oauth" not in created
        listed = client.get("/sources")
        assert "Add a Google calendar" in listed.text
    finally:
        _close(client)
        _close_server(server)


def test_full_import_keeps_fields_recurrence_and_etag(tmp_path: Path, monkeypatch) -> None:
    google = FakeGoogle()
    server, base = _server(google)
    client, fake = _open(tmp_path, monkeypatch, base)
    try:
        created = _create(client, "Series", google_calendar_id="team@example.com")
        _connect(client, created["id"])
        google.push(
            *_page(
                [
                    _item(
                        "series-1",
                        "etag-series",
                        summary="Weekly",
                        description="Bring notes",
                        location="Room 2",
                        start={"dateTime": "2026-10-05T09:00:30+02:00", "timeZone": "Europe/Amsterdam"},
                        end={"dateTime": "2026-10-05T10:00:30+02:00", "timeZone": "Europe/Amsterdam"},
                        recurrence=[
                            "RRULE:FREQ=WEEKLY;BYDAY=MO",
                            "EXDATE;TZID=Europe/Amsterdam:20261012T090030",
                        ],
                    ),
                    _item(
                        "series-1_moved",
                        "etag-moved",
                        summary="Weekly moved",
                        iCalUID="series-1@google.com",
                        recurringEventId="series-1",
                        originalStartTime={
                            "dateTime": "2026-10-19T09:00:30+02:00",
                            "timeZone": "Europe/Amsterdam",
                        },
                        start={"dateTime": "2026-10-19T11:00:00+02:00", "timeZone": "Europe/Amsterdam"},
                        end={"dateTime": "2026-10-19T12:00:00+02:00", "timeZone": "Europe/Amsterdam"},
                    ),
                    _item(
                        "day-1",
                        "etag-day",
                        summary="Holiday",
                        start={"date": "2026-10-07"},
                        end={"date": "2026-10-08"},
                    ),
                    _item(
                        "clock-1",
                        "etag-clock",
                        summary="Standup",
                        description="Seconds",
                        location="Desk",
                        status="tentative",
                    ),
                ],
                "sync-1",
            )
        )
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        calls = _calendar_calls(google)
        assert calls[0]["method"] == "GET"
        assert calls[0]["path"] == "/calendars/team%40example.com/events"
        assert calls[0]["params"]["singleEvents"] == "false"
        assert calls[0]["params"]["maxResults"] == "250"
        assert "syncToken" not in calls[0]["params"]
        assert all(item["method"] == "GET" for item in calls)
        series = _event_body(fake, "series-1@google.com")
        assert series.count("BEGIN:VEVENT") == 2
        assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in series
        assert "EXDATE;TZID=Europe/Amsterdam:20261012T090030" in series
        assert "RECURRENCE-ID;TZID=Europe/Amsterdam:20261019T090030" in series
        assert "SUMMARY:Weekly moved" in series
        assert "DESCRIPTION:Bring notes" in series
        assert "LOCATION:Room 2" in series
        day = _event_body(fake, "day-1@google.com")
        assert "DTSTART;VALUE=DATE:20261007" in day
        assert "DTEND;VALUE=DATE:20261008" in day
        assert "TZID" not in day
        clock = _event_body(fake, "clock-1@google.com")
        assert "DTSTART;TZID=Europe/Amsterdam:20261007T090030" in clock
        assert "STATUS:TENTATIVE" in clock
        assert "DESCRIPTION:Seconds" in clock
        rows = _mappings(client.app.state.engine, created["id"])
        assert {row["external_id"] for row in rows} == {"series-1", "day-1", "clock-1"}
        master = next(row for row in rows if row["external_id"] == "series-1")
        assert master["external_version"] == "etag-series"
        assert master["ical_uid"] == "series-1@google.com"
        assert master["deleted_at"] is None
        assert _state(client.app.state.engine, created["id"])["cursor"] == "sync-1"
        assert _state(client.app.state.engine, created["id"])["events_seen"] == 3
    finally:
        _close(client)
        _close_server(server)


def test_incremental_empty_delta_cancel_and_local_edit(tmp_path: Path, monkeypatch) -> None:
    google = FakeGoogle()
    server, base = _server(google)
    client, fake = _open(tmp_path, monkeypatch, base)
    try:
        created = _create(client)
        _connect(client, created["id"])
        google.push(*_page([_item("keep-1", "etag-keep", summary="Keep"), _item("gone-1", "etag-gone")], "sync-1"))
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        synced = {row["external_id"]: row["last_synced_at"] for row in _mappings(client.app.state.engine, created["id"])}

        google.push(*_page([_item("keep-1", "etag-keep-2", summary="Keep changed")], "sync-2"))
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        assert "SUMMARY:Keep changed" in _event_body(fake, "keep-1@google.com")
        rows = {row["external_id"]: row for row in _mappings(client.app.state.engine, created["id"])}
        assert rows["gone-1"]["last_synced_at"] == synced["gone-1"]
        assert rows["keep-1"]["external_version"] == "etag-keep-2"
        assert _calendar_calls(google)[-1]["params"]["syncToken"] == "sync-1"

        google.push(200, {"items": [], "nextSyncToken": "sync-3"})
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        empty = _state(client.app.state.engine, created["id"])
        assert empty["status"] == "ok"
        assert empty["cursor"] == "sync-3"
        assert empty["events_seen"] == 0
        assert empty["events_upserted"] == 0
        stamp = datetime.strptime(empty["last_success_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 60
        after_empty = {row["external_id"]: row["last_synced_at"] for row in _mappings(client.app.state.engine, created["id"])}
        assert after_empty["keep-1"] == rows["keep-1"]["last_synced_at"]
        assert after_empty["gone-1"] == synced["gone-1"]

        path = next(key for key, body in fake.events.items() if b"UID:keep-1@google.com" in body)
        fake.events[path] = fake.events[path].replace(b"Keep changed", b"Local edit")
        google.push(*_page([_item("keep-1", "etag-keep-2", summary="Keep changed")], "sync-4"))
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        assert "SUMMARY:Keep changed" in _event_body(fake, "keep-1@google.com")
        assert b"Local edit" not in fake.events[path]

        collection = created["collection_path"]
        fake.events[f"{collection}local.ics"] = b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:local-only\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        google.push(
            200,
            {
                "items": [_item("gone-1", "etag-gone", status="cancelled")],
                "nextSyncToken": "sync-5",
            },
        )
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        gone = next(row for row in _mappings(client.app.state.engine, created["id"]) if row["external_id"] == "gone-1")
        assert gone["deleted_at"] is not None
        assert all(b"UID:gone-1@google.com" not in body for body in fake.events.values())
        assert b"UID:local-only" in fake.events[f"{collection}local.ics"]
        assert all(item["method"] == "GET" for item in _calendar_calls(google))
    finally:
        _close(client)
        _close_server(server)


def test_exception_merges_and_a_cancelled_instance_becomes_exdate(tmp_path: Path, monkeypatch) -> None:
    google = FakeGoogle()
    server, base = _server(google)
    client, fake = _open(tmp_path, monkeypatch, base)
    try:
        created = _create(client)
        _connect(client, created["id"])
        google.push(
            *_page(
                [
                    _item(
                        "series-1",
                        "etag-series",
                        summary="Weekly",
                        recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
                    )
                ],
                "sync-1",
            )
        )
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        google.push(
            *_page(
                [
                    _item(
                        "series-1_moved",
                        "etag-moved",
                        summary="Moved once",
                        iCalUID="series-1@google.com",
                        recurringEventId="series-1",
                        originalStartTime={
                            "dateTime": "2026-10-19T09:00:30+02:00",
                            "timeZone": "Europe/Amsterdam",
                        },
                        start={"dateTime": "2026-10-19T11:00:00+02:00", "timeZone": "Europe/Amsterdam"},
                        end={"dateTime": "2026-10-19T12:00:00+02:00", "timeZone": "Europe/Amsterdam"},
                    )
                ],
                "sync-2",
            )
        )
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        series = _event_body(fake, "series-1@google.com")
        assert "SUMMARY:Weekly" in series
        assert "SUMMARY:Moved once" in series
        assert "RECURRENCE-ID;TZID=Europe/Amsterdam:20261019T090030" in series
        assert {row["external_id"] for row in _mappings(client.app.state.engine, created["id"])} == {"series-1"}

        google.push(
            *_page(
                [
                    {
                        "id": "series-1_drop",
                        "etag": "etag-drop",
                        "status": "cancelled",
                        "recurringEventId": "series-1",
                        "iCalUID": "series-1@google.com",
                        "originalStartTime": {
                            "dateTime": "2026-10-26T09:00:30+02:00",
                            "timeZone": "Europe/Amsterdam",
                        },
                    }
                ],
                "sync-3",
            )
        )
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        series = _event_body(fake, "series-1@google.com")
        # 26 Oct 2026 is after Amsterdam leaves summer time, so 09:00+02 is 08:00 there.
        assert "EXDATE;TZID=Europe/Amsterdam:20261026T080030" in series
        assert "SUMMARY:Weekly" in series
        row = _mappings(client.app.state.engine, created["id"])[0]
        assert row["deleted_at"] is None
        assert row["external_id"] == "series-1"
    finally:
        _close(client)
        _close_server(server)


def test_invalid_token_recovers_and_a_broken_page_keeps_events(tmp_path: Path, monkeypatch) -> None:
    google = FakeGoogle()
    server, base = _server(google)
    client, fake = _open(tmp_path, monkeypatch, base)
    try:
        created = _create(client)
        _connect(client, created["id"])
        google.push(*_page([_item("keep-1", "etag-keep"), _item("drop-1", "etag-drop")], "sync-1"))
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        google.push(410, {"error": "gone"})
        google.push(500, {"error": "down"})
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        failed = _state(client.app.state.engine, created["id"])
        assert failed["cursor"] is None
        assert failed["last_success_at"] is not None
        assert b"UID:keep-1@google.com" in b"".join(fake.events.values())
        assert b"UID:drop-1@google.com" in b"".join(fake.events.values())
        assert not any(call[0] == "delete-event" for call in fake.calls)
        recovery = _calendar_calls(google)
        assert recovery[-2]["params"]["syncToken"] == "sync-1"
        assert "syncToken" not in recovery[-1]["params"]

        google.push(*_page([_item("keep-1", "etag-keep")], "sync-2"))
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        rows = {row["external_id"]: row for row in _mappings(client.app.state.engine, created["id"])}
        assert rows["drop-1"]["deleted_at"] is not None
        assert rows["keep-1"]["deleted_at"] is None
        assert all(b"UID:drop-1@google.com" not in body for body in fake.events.values())
        assert _state(client.app.state.engine, created["id"])["cursor"] == "sync-2"

        google.push(200, {"items": [_item("new-1", "etag-new")], "nextPageToken": "page-2"})
        google.push(500, {"error": "page"})
        puts = len([call for call in fake.calls if call[0] == "put"])
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        broken = _state(client.app.state.engine, created["id"])
        assert broken["cursor"] == "sync-2"
        assert _calendar_calls(google)[-1]["params"]["pageToken"] == "page-2"
        assert "syncToken" not in _calendar_calls(google)[-1]["params"]
        assert len([call for call in fake.calls if call[0] == "put"]) == puts
        assert b"UID:keep-1@google.com" in b"".join(fake.events.values())
        assert not any(row["external_id"] == "new-1" for row in _mappings(client.app.state.engine, created["id"]))
    finally:
        _close(client)
        _close_server(server)


def test_a_failed_write_does_not_delete_the_rest(tmp_path: Path, monkeypatch) -> None:
    google = FakeGoogle()
    server, base = _server(google)
    client, fake = _open(tmp_path, monkeypatch, base)
    try:
        created = _create(client)
        _connect(client, created["id"])
        google.push(*_page([_item("keep-1", "etag-keep")], "sync-1"))
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        with Session(client.app.state.engine) as session, session.begin():
            session.get(SyncState, created["id"]).cursor = None
        fake.fail_after = len([call for call in fake.calls if call[0] == "put"]) + 1
        google.push(*_page([_item("new-1", "etag-new"), _item("new-2", "etag-new-2")], "sync-2"))
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "error"
        assert _state(client.app.state.engine, created["id"])["cursor"] is None
        assert b"UID:keep-1@google.com" in b"".join(fake.events.values())
        assert any(row["external_id"] == "new-1" for row in _mappings(client.app.state.engine, created["id"]))
        assert not any(row["external_id"] == "new-2" for row in _mappings(client.app.state.engine, created["id"]))
        keep = next(row for row in _mappings(client.app.state.engine, created["id"]) if row["external_id"] == "keep-1")
        assert keep["deleted_at"] is None
        assert not any(call[0] == "delete-event" for call in fake.calls)
    finally:
        _close(client)
        _close_server(server)


def test_access_token_is_refreshed_before_it_expires(tmp_path: Path, monkeypatch) -> None:
    google = FakeGoogle()
    server, base = _server(google)
    client, _fake = _open(tmp_path, monkeypatch, base)
    try:
        created = _create(client)
        _connect(client, created["id"])
        with Session(client.app.state.engine) as session, session.begin():
            session.get(SourceSecret, created["id"]).oauth_expiry = encrypt("2000-01-01T00:00:00Z")
        google.token_payload = {
            "access_token": "access-refreshed",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": READONLY_SCOPE,
        }
        google.push(200, {"items": [], "nextSyncToken": "sync-r"})
        before = len([item for item in google.requests if item["path"] == "/token"])
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        refresh = [item for item in google.requests if item["path"] == "/token"]
        assert len(refresh) == before + 1
        with Session(client.app.state.engine) as session:
            secret = session.get(SourceSecret, created["id"])
            assert decrypt(secret.oauth_access_token) == "access-refreshed"
            assert decrypt(secret.oauth_refresh_token) == "refresh-marker-value"
        assert _state(client.app.state.engine, created["id"])["cursor"] == "sync-r"
    finally:
        _close(client)
        _close_server(server)


def test_poll_clock_and_timezone_apply_to_google(tmp_path: Path, monkeypatch) -> None:
    google = FakeGoogle()
    server, base = _server(google)
    client, fake = _open(tmp_path, monkeypatch, base)
    try:
        created = _create(client, poll_seconds=3600)
        engine = client.app.state.engine
        moment = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
        with Session(engine) as session, session.begin():
            state = session.get(SyncState, created["id"])
            state.status = "ok"
            state.last_success_at = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert due_source_ids(engine, moment + timedelta(seconds=3599)) == []
        client.patch(f"/api/sources/{created['id']}", json={"enabled": 0}, headers=JSON)
        assert due_source_ids(engine, moment + timedelta(seconds=3600)) == []
        client.patch(f"/api/sources/{created['id']}", json={"enabled": 1}, headers=JSON)
        assert due_source_ids(engine, moment + timedelta(seconds=3600)) == [created["id"]]

        _connect(client, created["id"])
        google.push(
            *_page(
                [
                    _item("clock-1", "etag-clock"),
                    _item(
                        "day-1",
                        "etag-day",
                        summary="Holiday",
                        start={"date": "2026-10-07"},
                        end={"date": "2026-10-08"},
                    ),
                ],
                "sync-1",
            )
        )
        before = len(client.get("/api/sources").json()["sources"])
        client.app.state.scheduler.tick(moment + timedelta(seconds=3600))
        assert wait_for(engine, created["id"]) == "ok"
        assert len(client.get("/api/sources").json()["sources"]) == before
        assert client.get(f"/api/sources/{created['id']}").json()["poll_seconds"] == 3600
        assert "DTSTART;TZID=Europe/Amsterdam:20261007T090030" in _event_body(fake, "clock-1@google.com")

        changed = client.patch(
            f"/api/sources/{created['id']}",
            json={"timezone": "America/New_York"},
            headers=JSON,
        )
        assert changed.status_code == 200
        assert _state(engine, created["id"])["cursor"] is None
        google.push(
            *_page(
                [
                    _item("clock-1", "etag-clock"),
                    _item(
                        "day-1",
                        "etag-day",
                        summary="Holiday",
                        start={"date": "2026-10-07"},
                        end={"date": "2026-10-08"},
                    ),
                ],
                "sync-2",
            )
        )
        assert _sync(client, created["id"]).status_code == 202
        assert wait_for(engine, created["id"]) == "ok"
        assert "DTSTART;TZID=America/New_York:20261007T030030" in _event_body(fake, "clock-1@google.com")
        assert "DTSTART;VALUE=DATE:20261007" in _event_body(fake, "day-1@google.com")
        assert "syncToken" not in _calendar_calls(google)[-1]["params"]

        kept = client.patch(
            f"/api/sources/{created['id']}",
            json={"poll_seconds": 120},
            headers=JSON,
        )
        assert kept.status_code == 200
        assert _state(engine, created["id"])["cursor"] == "sync-2"
        cleared = client.patch(
            f"/api/sources/{created['id']}",
            json={"google_calendar_id": "other"},
            headers=JSON,
        )
        assert cleared.status_code == 200
        assert _state(engine, created["id"])["cursor"] is None
    finally:
        _close(client)
        _close_server(server)
