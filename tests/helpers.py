"""Shared helpers for ICS and webhook HTTP tests."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests.fakes import FakeRadicale

from app.db.models import EventMapping, SourceSecret, SyncState
from app.secrets import decrypt

JSON = {"Content-Type": "application/json"}
HX = {"HX-Request": "true"}
WEBHOOK_MARKER = "ZzMarkerTitle"


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


def start_feed_server(feed: Feed) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), feed.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}"


def close_feed_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def make_calendar(*events: str) -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", *events, "END:VCALENDAR"]
    return ("\r\n".join(lines) + "\r\n").encode()


def make_event(uid: str, **lines: str) -> list[str]:
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


def create_ics_source(client: TestClient, url: str, **extra) -> dict:
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


def trigger_sync(client: TestClient, source_id: str):
    return client.post(f"/api/sources/{source_id}/sync", json={}, headers=JSON)


def create_webhook_source(
    client: TestClient, name: str = "Hook", enabled: int = 1
) -> dict:
    response = client.post(
        "/api/sources",
        json={"name": name, "kind": "webhook", "enabled": enabled},
        headers=JSON,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "webhook_secret" not in body
    assert body["kind"] == "webhook"
    assert body["color"] == "#9334e6"
    assert body["kind_label"] == "Webhook"
    assert body["status"] == "idle"
    assert body["status_label"] == "not yet started"
    return body


def webhook_secret(engine, source_id: str) -> str:
    with Session(engine) as session:
        row = session.get(SourceSecret, source_id)
        assert row is not None and row.webhook_secret
        plain = decrypt(row.webhook_secret)
        assert plain not in row.webhook_secret
        return plain


def webhook_payload(**extra) -> dict:
    body = {
        "id": "ext-1",
        "action": "upsert",
        "summary": WEBHOOK_MARKER,
        "description": "notes",
        "location": "room",
        "start": "2026-10-07T09:00:15",
        "end": "2026-10-07T10:00:15",
        "all_day": False,
        "timezone": "Europe/Amsterdam",
        "rrule": "FREQ=WEEKLY;BYDAY=MO",
        "status": "confirmed",
    }
    body.update(extra)
    return body


def send_webhook(client: TestClient, source_id: str, secret: str, body: dict):
    return client.post(
        f"/api/webhooks/{source_id}",
        json=body,
        headers={"X-Webhook-Secret": secret},
    )


def source_mappings(engine, source_id: str) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(
            select(EventMapping).where(EventMapping.source_id == source_id)
        ).all()
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


def source_state(engine, source_id: str) -> dict:
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


def put_calls(fake: FakeRadicale) -> list[tuple]:
    return [call for call in fake.calls if call[0] == "put"]


def mapping_for(mappings: list[dict], external_id: str) -> dict:
    return next(row for row in mappings if row["external_id"] == external_id)


def event_path(collection: str, mappings: list[dict], external_id: str) -> str:
    return f"{collection}{mapping_for(mappings, external_id)['ical_uid']}.ics"


def event_body(
    fake: FakeRadicale, collection: str, mappings: list[dict], external_id: str
) -> str:
    return fake.events[event_path(collection, mappings, external_id)].decode()
