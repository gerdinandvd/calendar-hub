"""Webhook import, secrets, and the sync page."""

import base64
import re
import threading
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.services.sources import ensure_sync_states
from app.db.models import EventMapping, SourceSecret, SyncState
from app.main import create_app
from app.radicale.client import RadicaleError
from app.secrets import decrypt
from app.sync.apply import CAPACITY_MESSAGE, MAX_EVENTS, RunStats, SAVE_MESSAGE
from app.ui.text import english
from app.sync.runner import SlotBusy, run_source
from app.sync.webhook import parse

JSON = {"Content-Type": "application/json"}
HX = {"HX-Request": "true"}
SECRET_KEY = base64.urlsafe_b64encode(b"calendar-hub-local-dev-key-32b!!").decode()
MARKER = "ZzMarkerTitle"


class FakeRadicale:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.events: dict[str, bytes] = {}
        self.fail_put = False
        self.block_put: threading.Event | None = None
        self.put_started = threading.Event()

    def create_collection(self, path: str, display_name: str, color: str) -> None:
        self.calls.append(("create", path, display_name, color))

    def update_properties(self, path: str, display_name: str, color: str) -> None:
        self.calls.append(("proppatch", path, display_name, color))

    def delete_collection(self, path: str) -> None:
        self.calls.append(("delete", path))

    def put_event(self, path: str, body: bytes, *, fresh: bool = False) -> None:
        self.calls.append(("put", path, body))
        if self.block_put is not None:
            self.put_started.set()
            self.block_put.wait(5)
        if self.fail_put:
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
        return sum(1 for key in self.events if key.startswith(prefix) and key.lower().endswith(".ics"))


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
    fake = FakeRadicale()
    client.app.state.radicale = fake
    return client, fake


def _close(client: TestClient) -> None:
    client.__exit__(None, None, None)


def _webhook(client: TestClient, name: str = "Hook", enabled: int = 1) -> dict:
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


def _plain_secret(engine, source_id: str) -> str:
    with Session(engine) as session:
        row = session.get(SourceSecret, source_id)
        assert row is not None and row.webhook_secret
        plain = decrypt(row.webhook_secret)
        assert plain not in row.webhook_secret
        return plain


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


def _state(engine, source_id: str) -> dict:
    with Session(engine) as session:
        row = session.get(SyncState, source_id)
        assert row is not None
        return {
            "status": row.status,
            "last_success_at": row.last_success_at,
            "last_error_at": row.last_error_at,
            "last_error_message": row.last_error_message,
            "events_seen": row.events_seen,
            "events_upserted": row.events_upserted,
            "events_deleted": row.events_deleted,
        }


def _payload(**extra) -> dict:
    body = {
        "id": "ext-1",
        "action": "upsert",
        "summary": MARKER,
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


def _send(client: TestClient, source_id: str, secret: str, body: dict):
    return client.post(
        f"/api/webhooks/{source_id}",
        json=body,
        headers={"X-Webhook-Secret": secret},
    )


def _puts(fake: FakeRadicale) -> list[tuple]:
    return [call for call in fake.calls if call[0] == "put"]


def test_upsert_is_idempotent_and_stores_the_revision(tmp_path: Path, monkeypatch, caplog) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        created = _webhook(client)
        secret = _plain_secret(client.app.state.engine, created["id"])
        payload = _payload()
        reordered = {key: payload[key] for key in reversed(list(payload))}
        assert parse(payload, "Europe/Amsterdam").revision == parse(reordered, "Europe/Amsterdam").revision

        with caplog.at_level("INFO", logger="app"):
            first = _send(client, created["id"], secret, payload)
        assert first.status_code == 200, first.text
        assert first.json() == {"ok": True}
        assert len(_puts(fake)) == 1
        path, body = _puts(fake)[0][1], _puts(fake)[0][2]
        text = body.decode()
        assert "DTSTART;TZID=Europe/Amsterdam:20261007T090015" in text
        assert "DTEND;TZID=Europe/Amsterdam:20261007T100015" in text
        assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in text
        assert "STATUS:CONFIRMED" in text
        assert f"SUMMARY:{MARKER}" in text
        assert "action" not in text.lower()
        mapping = _mappings(client.app.state.engine, created["id"])
        assert len(mapping) == 1
        assert mapping[0]["external_version"] == parse(payload, "Europe/Amsterdam").revision
        assert mapping[0]["ical_uid"] in text
        assert mapping[0]["deleted_at"] is None
        synced_at = mapping[0]["last_synced_at"]
        state = _state(client.app.state.engine, created["id"])
        assert state["status"] == "ok"
        assert state["last_success_at"]
        assert state["events_seen"] == 1
        assert state["events_upserted"] == 1
        assert state["events_deleted"] == 0

        second = _send(client, created["id"], secret, payload)
        assert second.status_code == 200
        assert len(_puts(fake)) == 1
        again = _mappings(client.app.state.engine, created["id"])
        assert again[0]["ical_uid"] == mapping[0]["ical_uid"]
        assert again[0]["last_synced_at"] == synced_at
        assert again[0]["external_version"] == mapping[0]["external_version"]

        changed = _payload(summary="Other title")
        third = _send(client, created["id"], secret, changed)
        assert third.status_code == 200
        assert len(_puts(fake)) == 2
        updated = _mappings(client.app.state.engine, created["id"])
        assert len(updated) == 1
        assert updated[0]["ical_uid"] == mapping[0]["ical_uid"]
        assert updated[0]["external_version"] != mapping[0]["external_version"]
        assert b"Other title" in fake.events[path]
        assert MARKER.encode() not in fake.events[path]
        assert secret not in caplog.text
        assert MARKER not in caplog.text
        assert "actor=webhook" in caplog.text
        assert f"source_id={created['id']}" in caplog.text
        assert "run_id=" in caplog.text
        assert "outcome=ok" in caplog.text
    finally:
        _close(client)


def test_all_day_and_minutes_without_seconds(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        created = _webhook(client)
        secret = _plain_secret(client.app.state.engine, created["id"])
        day = _send(
            client,
            created["id"],
            secret,
            {"id": "day-1", "action": "upsert", "summary": "Away", "start": "2026-10-07", "all_day": True},
        )
        assert day.status_code == 200, day.text
        stored = next(iter(fake.events.values())).decode()
        assert "DTSTART;VALUE=DATE:20261007" in stored
        assert "DTEND" not in stored
        assert "TZID" not in stored
        assert "VALUE=DATE:20261006" not in stored
        assert "VALUE=DATE:20261008" not in stored

        clock = _send(
            client,
            created["id"],
            secret,
            {
                "id": "clock-1",
                "action": "upsert",
                "summary": "Hour",
                "start": "2026-10-07T09:00",
                "end": "2026-10-07T10:00",
            },
        )
        assert clock.status_code == 200, clock.text
        text = fake.events[_puts(fake)[-1][1]].decode()
        assert "DTSTART;TZID=Europe/Amsterdam:20261007T0900\r\n" in text
        assert "T090000" not in text
        assert "T090015" not in text
    finally:
        _close(client)


def test_delete_known_id_and_leave_unknown_and_unmapped(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        created = _webhook(client)
        secret = _plain_secret(client.app.state.engine, created["id"])
        assert _send(client, created["id"], secret, _payload()).status_code == 200
        mapping = _mappings(client.app.state.engine, created["id"])[0]
        path = _puts(fake)[0][1]
        local = created["collection_path"] + "local.ics"
        fake.events[local] = b"LOCAL-EVENT"

        missing = _send(client, created["id"], secret, {"id": "missing", "action": "delete"})
        assert missing.status_code == 200
        assert missing.json() == {"ok": True}
        assert not any(call[0] == "delete-event" for call in fake.calls)
        assert len(_mappings(client.app.state.engine, created["id"])) == 1
        assert path in fake.events

        removed = _send(client, created["id"], secret, {"id": "ext-1", "action": "delete"})
        assert removed.status_code == 200
        assert path not in fake.events
        assert fake.events[local] == b"LOCAL-EVENT"
        deleted = _mappings(client.app.state.engine, created["id"])[0]
        assert deleted["ical_uid"] == mapping["ical_uid"]
        assert deleted["deleted_at"] is not None
        state = _state(client.app.state.engine, created["id"])
        assert state["status"] == "ok"
        assert state["events_deleted"] == 1

        restored = _send(client, created["id"], secret, _payload())
        assert restored.status_code == 200
        revived = _mappings(client.app.state.engine, created["id"])[0]
        assert revived["ical_uid"] == mapping["ical_uid"]
        assert revived["deleted_at"] is None
        assert path in fake.events
    finally:
        _close(client)


def test_invalid_webhook_and_disabled_source_do_not_write(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        created = _webhook(client)
        secret = _plain_secret(client.app.state.engine, created["id"])
        assert _send(client, created["id"], secret, _payload()).status_code == 200
        path = _puts(fake)[0][1]
        stored = fake.events[path]
        success = _state(client.app.state.engine, created["id"])

        wrong = _send(client, created["id"], "not-the-secret", _payload(summary="Nope"))
        assert wrong.status_code == 401
        assert wrong.json()["error"] == "invalid_secret"
        assert "Traceback" not in wrong.text
        missing_header = client.post(f"/api/webhooks/{created['id']}", json=_payload())
        assert missing_header.status_code == 401
        invalid = _send(client, created["id"], secret, {"id": "ext-1", "action": "upsert", "summary": "x"})
        assert invalid.status_code == 422
        assert invalid.json()["error"] == "invalid_request"
        extra = _send(client, created["id"], secret, _payload(color="red"))
        assert extra.status_code == 422
        assert fake.events[path] == stored
        assert len(_puts(fake)) == 1
        assert _state(client.app.state.engine, created["id"])["status"] == "ok"
        assert _state(client.app.state.engine, created["id"])["last_success_at"] == success["last_success_at"]

        fresh = _webhook(client, "Other hook")
        fresh_secret = _plain_secret(client.app.state.engine, fresh["id"])
        bad = _send(client, fresh["id"], fresh_secret, {"action": "upsert"})
        assert bad.status_code == 422
        assert _state(client.app.state.engine, fresh["id"])["status"] == "idle"
        assert _mappings(client.app.state.engine, fresh["id"]) == []

        client.patch(f"/api/sources/{fresh['id']}", json={"enabled": 0}, headers=JSON)
        disabled = _send(client, fresh["id"], fresh_secret, _payload(id="ext-2"))
        assert disabled.status_code == 422
        assert disabled.json()["error"] == "sync_disabled"
        assert _state(client.app.state.engine, fresh["id"])["status"] == "idle"
        assert _state(client.app.state.engine, fresh["id"])["last_success_at"] is None
        assert len(_puts(fake)) == 1

        private = client.post("/api/sources", json={"name": "Home"}, headers=JSON).json()
        refused = _send(client, private["id"], secret, _payload())
        assert refused.status_code == 404
        assert len(_puts(fake)) == 1
    finally:
        _close(client)


def test_limits_reject_without_a_write(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        created = _webhook(client)
        secret = _plain_secret(client.app.state.engine, created["id"])
        huge = client.post(
            f"/api/webhooks/{created['id']}",
            content=b"x" * (1024 * 1024),
            headers={"X-Webhook-Secret": secret},
        )
        assert huge.status_code == 413
        assert huge.json()["error"] == "payload_too_large"
        assert "Traceback" not in huge.text
        assert fake.events == {}

        for number in range(60):
            response = _send(client, created["id"], "wrong", _payload())
            assert response.status_code == 401, number
        limited = _send(client, created["id"], secret, _payload())
        assert limited.status_code == 429
        assert limited.json()["error"] == "rate_limited"
        assert fake.events == {}
        assert _state(client.app.state.engine, created["id"])["status"] == "idle"
    finally:
        _close(client)


def test_failed_write_keeps_the_collection(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        created = _webhook(client)
        secret = _plain_secret(client.app.state.engine, created["id"])
        assert _send(client, created["id"], secret, _payload()).status_code == 200
        path = _puts(fake)[0][1]
        stored = fake.events[path]
        success_at = _state(client.app.state.engine, created["id"])["last_success_at"]
        synced_at = _mappings(client.app.state.engine, created["id"])[0]["last_synced_at"]
        local = created["collection_path"] + "local.ics"
        fake.events[local] = b"LOCAL-EVENT"
        fake.fail_put = True

        failed = _send(client, created["id"], secret, _payload(id="ext-2", summary="Broken"))
        assert failed.status_code == 500
        assert failed.json()["error"] == "internal_error"
        assert failed.json()["detail"] == "The event could not be saved."
        assert "Traceback" not in failed.text
        assert fake.events[path] == stored
        assert fake.events[local] == b"LOCAL-EVENT"
        state = _state(client.app.state.engine, created["id"])
        assert state["status"] == "error"
        assert state["last_success_at"] == success_at
        assert state["last_error_message"] == SAVE_MESSAGE
        assert state["events_upserted"] == 1
        assert state["events_seen"] == 1
        assert _mappings(client.app.state.engine, created["id"])[0]["last_synced_at"] == synced_at
        assert len(_mappings(client.app.state.engine, created["id"])) == 1
    finally:
        _close(client)


def test_secret_is_shown_once_and_rotation_replaces_it(tmp_path: Path, monkeypatch, caplog) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        form = client.get("/sources/new?kind=webhook")
        assert 'for="name"' in form.text
        assert ">Webhook<" in form.text
        assert "Time zone" not in form.text
        assert "poll" not in form.text.lower()

        with caplog.at_level("INFO", logger="app"):
            created = client.post("/sources?kind=webhook", data={"name": "Hook"}, headers=HX)
        assert created.status_code == 200
        assert created.headers["cache-control"] == "no-store"
        secret = re.search(r'id="webhook-secret-value"[^>]*>([^<]+)', created.text)
        assert secret is not None
        shown = secret.group(1)
        source_id = re.search(r"/api/webhooks/([^<]+)", created.text).group(1)
        assert shown not in client.get("/sources").text
        assert shown not in client.get("/api/sources").text
        assert shown not in client.get("/").text
        assert shown not in caplog.text

        edit = client.get(f"/sources/{source_id}")
        assert "Secret is set." in edit.text
        assert shown not in edit.text
        assert "Replace secret" in edit.text
        assert f"https://localhost:8443/api/webhooks/{source_id}" in edit.text
        assert "Time zone" in edit.text
        before = client.get(f"/api/sources/{source_id}").json()["collection_url"]

        rotated = client.post(f"/sources/{source_id}/rotate-secret", headers=HX)
        assert rotated.status_code == 200
        assert rotated.headers["cache-control"] == "no-store"
        new_secret = re.search(r'id="webhook-secret-value"[^>]*>([^<]+)', rotated.text).group(1)
        assert new_secret != shown
        assert "The previous secret no longer works." in rotated.text
        assert client.get(f"/sources/{source_id}").text.find(new_secret) == -1
        assert client.get(f"/api/sources/{source_id}").json()["collection_url"] == before

        old = _send(client, source_id, shown, _payload())
        assert old.status_code == 401
        assert fake.events == {}
        accepted = _send(client, source_id, new_secret, _payload())
        assert accepted.status_code == 200
        assert len(fake.events) == 1

        private = client.post("/api/sources", json={"name": "Home"}, headers=JSON).json()
        refused = client.post(f"/api/sources/{private['id']}/rotate-secret", headers=JSON)
        assert refused.status_code == 422
        assert refused.json()["error"] == "rotate_not_supported"

        listed = client.get(f"/api/sources/{source_id}").json()
        json_rotate = client.post(f"/api/sources/{source_id}/rotate-secret", headers=JSON)
        assert json_rotate.status_code == 200
        assert json_rotate.json() == {"id": source_id, "rotated": True}
        plain = _plain_secret(client.app.state.engine, source_id)
        assert plain not in json_rotate.text
        assert plain != new_secret
        assert client.get(f"/api/sources/{source_id}").json()["collection_url"] == listed["collection_url"]
        assert len(_mappings(client.app.state.engine, source_id)) == 1

        removed = client.delete(f"/api/sources/{source_id}?confirm=true", headers=JSON)
        assert removed.status_code == 200
        assert _mappings(client.app.state.engine, source_id) == []
        with Session(client.app.state.engine) as session:
            assert session.get(SourceSecret, source_id) is None
            assert session.get(SyncState, source_id) is None
    finally:
        _close(client)


def test_sync_page_shows_success_and_error(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        private = client.post("/api/sources", json={"name": "Home"}, headers=JSON)
        assert private.status_code == 201
        ok_source = _webhook(client, "Ok hook")
        secret = _plain_secret(client.app.state.engine, ok_source["id"])
        assert _send(client, ok_source["id"], secret, _payload()).status_code == 200
        bad = _webhook(client, "Bad hook")
        bad_secret = _plain_secret(client.app.state.engine, bad["id"])
        fake.fail_put = True
        failed = _send(client, bad["id"], bad_secret, _payload(id="ext-9"))
        assert failed.status_code == 500

        page = client.get("/sync")
        assert page.status_code == 200
        assert "succeeded" in page.text
        assert "The event could not be saved." in page.text
        assert "no sync" in page.text
        assert "Sync now" not in page.text
        assert 'hx-trigger="every 10s"' in page.text
        panel = client.get("/sync/panel")
        assert "succeeded" in panel.text
        assert "The event could not be saved." in panel.text
        assert "no sync" in panel.text

        status = client.get("/api/sync")
        assert status.status_code == 200
        rows = {item["name"]: item for item in status.json()["sources"]}
        assert rows["Home"]["status"] == "na"
        assert rows["Home"]["last_success_at"] is None
        assert rows["Home"]["events_seen"] is None
        assert rows["Ok hook"]["status"] == "ok"
        assert rows["Ok hook"]["last_success_at"]
        assert rows["Bad hook"]["status"] == "error"
        assert rows["Bad hook"]["last_error_message"] == SAVE_MESSAGE
        assert rows["Bad hook"]["last_success_at"] is None
        assert secret not in status.text
        assert MARKER not in page.text
    finally:
        _close(client)


def test_missing_sync_row_is_filled_for_an_existing_private_source(tmp_path: Path, monkeypatch) -> None:
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        created = client.post("/api/sources", json={"name": "Home"}, headers=JSON).json()
        with Session(client.app.state.engine) as session, session.begin():
            session.execute(delete(SyncState).where(SyncState.source_id == created["id"]))
        ensure_sync_states(client.app.state.engine)
        again = client.get(f"/api/sources/{created['id']}").json()
        assert again["status"] == "na"
        assert again["status_label"] == "not applicable"
    finally:
        _close(client)


def test_a_second_run_does_not_start_while_the_slot_is_held(tmp_path: Path, monkeypatch) -> None:
    client, _fake = _open(tmp_path, monkeypatch)
    try:
        created = _webhook(client)
        started = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []

        def work() -> RunStats:
            started.set()
            assert release.wait(2)
            return RunStats(seen=1)

        def hold() -> None:
            try:
                run_source(
                    client.app.state.engine,
                    created["id"],
                    actor="webhook",
                    work=work,
                    wait=True,
                )
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=hold)
        thread.start()
        assert started.wait(2)
        try:
            run_source(
                client.app.state.engine,
                created["id"],
                actor="webhook",
                work=lambda: RunStats(),
                wait=False,
            )
            busy = False
        except SlotBusy:
            busy = True
        release.set()
        thread.join(2)
        assert busy
        assert errors == []
        assert _state(client.app.state.engine, created["id"])["status"] == "ok"
    finally:
        _close(client)


def test_webhook_stops_at_the_event_limit(tmp_path: Path, monkeypatch) -> None:
    client, fake = _open(tmp_path, monkeypatch)
    try:
        created = _webhook(client, "Full")
        secret = _plain_secret(client.app.state.engine, created["id"])
        assert _send(client, created["id"], secret, _payload()).status_code == 200
        success = _state(client.app.state.engine, created["id"])
        collection = created["collection_path"]
        for number in range(MAX_EVENTS - 1):
            fake.events[f"{collection}pad-{number}.ics"] = b"x"
        assert fake.count_events(collection) == MAX_EVENTS
        puts = len(_puts(fake))
        refused = _send(client, created["id"], secret, _payload(id="extra", summary="Extra"))
        assert refused.status_code == 500
        assert refused.json()["detail"] == english(CAPACITY_MESSAGE)
        assert "Traceback" not in refused.text
        failed = _state(client.app.state.engine, created["id"])
        assert failed["status"] == "error"
        assert failed["last_error_message"] == CAPACITY_MESSAGE
        assert failed["last_success_at"] == success["last_success_at"]
        assert len(_puts(fake)) == puts
        assert [row["external_id"] for row in _mappings(client.app.state.engine, created["id"])] == ["ext-1"]

        updated = _send(client, created["id"], secret, _payload(summary="Changed title"))
        assert updated.status_code == 200
        assert _state(client.app.state.engine, created["id"])["status"] == "ok"
        assert b"Changed title" in next(iter(fake.events.values())) or any(
            b"Changed title" in body for body in fake.events.values()
        )
    finally:
        _close(client)
