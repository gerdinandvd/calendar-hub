"""Webhook import, secrets, and the sync page."""

import re
import threading

from sqlalchemy import delete
from sqlalchemy.orm import Session
from tests.helpers import (
    HX,
    JSON,
    create_webhook_source,
    put_calls,
    send_webhook,
    source_mappings,
    source_state,
    webhook_payload,
    webhook_secret,
)
from tests.helpers import (
    WEBHOOK_MARKER as MARKER,
)

from app.db.models import SourceSecret, SyncState
from app.i18n import english
from app.services.sources.sync_ops import ensure_sync_states
from app.sync.apply import CAPACITY_MESSAGE, SAVE_MESSAGE, RunStats
from app.sync.runner import SlotBusy, run_source
from app.sync.webhook import parse


def test_upsert_is_idempotent_and_stores_the_revision(
    hub_client, fake_radicale, caplog
) -> None:
    client, fake = hub_client, fake_radicale
    created = create_webhook_source(client)
    secret = webhook_secret(client.app.state.engine, created["id"])
    payload = webhook_payload()
    reordered = {key: payload[key] for key in reversed(list(payload))}
    assert (
        parse(payload, "Europe/Amsterdam").revision
        == parse(reordered, "Europe/Amsterdam").revision
    )

    with caplog.at_level("INFO", logger="app"):
        first = send_webhook(client, created["id"], secret, payload)
    assert first.status_code == 200, first.text
    assert first.json() == {"ok": True}
    assert len(put_calls(fake)) == 1
    path, body = put_calls(fake)[0][1], put_calls(fake)[0][2]
    text = body.decode()
    assert "DTSTART;TZID=Europe/Amsterdam:20261007T090015" in text
    assert "DTEND;TZID=Europe/Amsterdam:20261007T100015" in text
    assert "RRULE:FREQ=WEEKLY;BYDAY=MO" in text
    assert "STATUS:CONFIRMED" in text
    assert f"SUMMARY:{MARKER}" in text
    assert "action" not in text.lower()
    mapping = source_mappings(client.app.state.engine, created["id"])
    assert len(mapping) == 1
    assert mapping[0]["external_version"] == parse(payload, "Europe/Amsterdam").revision
    assert mapping[0]["ical_uid"] in text
    assert mapping[0]["deleted_at"] is None
    synced_at = mapping[0]["last_synced_at"]
    state = source_state(client.app.state.engine, created["id"])
    assert state["status"] == "ok"
    assert state["last_success_at"]
    assert state["events_seen"] == 1
    assert state["events_upserted"] == 1
    assert state["events_deleted"] == 0

    second = send_webhook(client, created["id"], secret, payload)
    assert second.status_code == 200
    assert len(put_calls(fake)) == 1
    again = source_mappings(client.app.state.engine, created["id"])
    assert again[0]["ical_uid"] == mapping[0]["ical_uid"]
    assert again[0]["last_synced_at"] == synced_at
    assert again[0]["external_version"] == mapping[0]["external_version"]

    changed = webhook_payload(summary="Other title")
    third = send_webhook(client, created["id"], secret, changed)
    assert third.status_code == 200
    assert len(put_calls(fake)) == 2
    updated = source_mappings(client.app.state.engine, created["id"])
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


def test_all_day_and_minutes_without_seconds(hub_client, fake_radicale) -> None:
    client, fake = hub_client, fake_radicale
    created = create_webhook_source(client)
    secret = webhook_secret(client.app.state.engine, created["id"])
    day = send_webhook(
        client,
        created["id"],
        secret,
        {
            "id": "day-1",
            "action": "upsert",
            "summary": "Away",
            "start": "2026-10-07",
            "all_day": True,
        },
    )
    assert day.status_code == 200, day.text
    stored = next(iter(fake.events.values())).decode()
    assert "DTSTART;VALUE=DATE:20261007" in stored
    assert "DTEND" not in stored
    assert "TZID" not in stored
    assert "VALUE=DATE:20261006" not in stored
    assert "VALUE=DATE:20261008" not in stored

    clock = send_webhook(
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
    text = fake.events[put_calls(fake)[-1][1]].decode()
    assert "DTSTART;TZID=Europe/Amsterdam:20261007T0900\r\n" in text
    assert "T090000" not in text
    assert "T090015" not in text


def test_delete_known_id_and_leave_unknown_and_unmapped(
    hub_client, fake_radicale
) -> None:
    client, fake = hub_client, fake_radicale
    created = create_webhook_source(client)
    secret = webhook_secret(client.app.state.engine, created["id"])
    assert (
        send_webhook(client, created["id"], secret, webhook_payload()).status_code
        == 200
    )
    mapping = source_mappings(client.app.state.engine, created["id"])[0]
    path = put_calls(fake)[0][1]
    local = created["collection_path"] + "local.ics"
    fake.events[local] = b"LOCAL-EVENT"

    missing = send_webhook(
        client, created["id"], secret, {"id": "missing", "action": "delete"}
    )
    assert missing.status_code == 200
    assert missing.json() == {"ok": True}
    assert not any(call[0] == "delete-event" for call in fake.calls)
    assert len(source_mappings(client.app.state.engine, created["id"])) == 1
    assert path in fake.events

    removed = send_webhook(
        client, created["id"], secret, {"id": "ext-1", "action": "delete"}
    )
    assert removed.status_code == 200
    assert path not in fake.events
    assert fake.events[local] == b"LOCAL-EVENT"
    deleted = source_mappings(client.app.state.engine, created["id"])[0]
    assert deleted["ical_uid"] == mapping["ical_uid"]
    assert deleted["deleted_at"] is not None
    state = source_state(client.app.state.engine, created["id"])
    assert state["status"] == "ok"
    assert state["events_deleted"] == 1

    restored = send_webhook(client, created["id"], secret, webhook_payload())
    assert restored.status_code == 200
    revived = source_mappings(client.app.state.engine, created["id"])[0]
    assert revived["ical_uid"] == mapping["ical_uid"]
    assert revived["deleted_at"] is None
    assert path in fake.events


def test_invalid_webhook_and_disabled_source_do_not_write(
    hub_client, fake_radicale
) -> None:
    client, fake = hub_client, fake_radicale
    created = create_webhook_source(client)
    secret = webhook_secret(client.app.state.engine, created["id"])
    assert (
        send_webhook(client, created["id"], secret, webhook_payload()).status_code
        == 200
    )
    path = put_calls(fake)[0][1]
    stored = fake.events[path]
    success = source_state(client.app.state.engine, created["id"])

    wrong = send_webhook(
        client, created["id"], "not-the-secret", webhook_payload(summary="Nope")
    )
    assert wrong.status_code == 401
    assert wrong.json()["error"] == "invalid_secret"
    assert "Traceback" not in wrong.text
    missing_header = client.post(
        f"/api/webhooks/{created['id']}", json=webhook_payload()
    )
    assert missing_header.status_code == 401
    invalid = send_webhook(
        client,
        created["id"],
        secret,
        {"id": "ext-1", "action": "upsert", "summary": "x"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"] == "invalid_request"
    extra = send_webhook(client, created["id"], secret, webhook_payload(color="red"))
    assert extra.status_code == 422
    assert fake.events[path] == stored
    assert len(put_calls(fake)) == 1
    assert source_state(client.app.state.engine, created["id"])["status"] == "ok"
    assert (
        source_state(client.app.state.engine, created["id"])["last_success_at"]
        == success["last_success_at"]
    )

    fresh = create_webhook_source(client, "Other hook")
    fresh_secret = webhook_secret(client.app.state.engine, fresh["id"])
    bad = send_webhook(client, fresh["id"], fresh_secret, {"action": "upsert"})
    assert bad.status_code == 422
    assert source_state(client.app.state.engine, fresh["id"])["status"] == "idle"
    assert source_mappings(client.app.state.engine, fresh["id"]) == []

    client.patch(f"/api/sources/{fresh['id']}", json={"enabled": 0}, headers=JSON)
    disabled = send_webhook(
        client, fresh["id"], fresh_secret, webhook_payload(id="ext-2")
    )
    assert disabled.status_code == 422
    assert disabled.json()["error"] == "sync_disabled"
    assert source_state(client.app.state.engine, fresh["id"])["status"] == "idle"
    assert source_state(client.app.state.engine, fresh["id"])["last_success_at"] is None
    assert len(put_calls(fake)) == 1

    private = client.post("/api/sources", json={"name": "Home"}, headers=JSON).json()
    refused = send_webhook(client, private["id"], secret, webhook_payload())
    assert refused.status_code == 404
    assert len(put_calls(fake)) == 1


def test_limits_reject_without_a_write(hub_client, fake_radicale) -> None:
    client, fake = hub_client, fake_radicale
    created = create_webhook_source(client)
    secret = webhook_secret(client.app.state.engine, created["id"])
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
        response = send_webhook(client, created["id"], "wrong", webhook_payload())
        assert response.status_code == 401, number
    limited = send_webhook(client, created["id"], secret, webhook_payload())
    assert limited.status_code == 429
    assert limited.json()["error"] == "rate_limited"
    assert fake.events == {}
    assert source_state(client.app.state.engine, created["id"])["status"] == "idle"


def test_failed_write_keeps_the_collection(hub_client, fake_radicale) -> None:
    client, fake = hub_client, fake_radicale
    created = create_webhook_source(client)
    secret = webhook_secret(client.app.state.engine, created["id"])
    assert (
        send_webhook(client, created["id"], secret, webhook_payload()).status_code
        == 200
    )
    path = put_calls(fake)[0][1]
    stored = fake.events[path]
    success_at = source_state(client.app.state.engine, created["id"])["last_success_at"]
    synced_at = source_mappings(client.app.state.engine, created["id"])[0][
        "last_synced_at"
    ]
    local = created["collection_path"] + "local.ics"
    fake.events[local] = b"LOCAL-EVENT"
    fake.fail_put = True

    failed = send_webhook(
        client, created["id"], secret, webhook_payload(id="ext-2", summary="Broken")
    )
    assert failed.status_code == 500
    assert failed.json()["error"] == "internal_error"
    assert failed.json()["detail"] == "The event could not be saved."
    assert "Traceback" not in failed.text
    assert fake.events[path] == stored
    assert fake.events[local] == b"LOCAL-EVENT"
    state = source_state(client.app.state.engine, created["id"])
    assert state["status"] == "error"
    assert state["last_success_at"] == success_at
    assert state["last_error_message"] == SAVE_MESSAGE
    assert state["events_upserted"] == 1
    assert state["events_seen"] == 1
    assert (
        source_mappings(client.app.state.engine, created["id"])[0]["last_synced_at"]
        == synced_at
    )
    assert len(source_mappings(client.app.state.engine, created["id"])) == 1


def test_secret_is_shown_once_and_rotation_replaces_it(
    hub_client, fake_radicale, caplog
) -> None:
    client, fake = hub_client, fake_radicale
    form = client.get("/sources/new?kind=webhook")
    assert 'for="name"' in form.text
    assert ">Webhook<" in form.text
    assert "Time zone" not in form.text
    assert "poll" not in form.text.lower()

    with caplog.at_level("INFO", logger="app"):
        created = client.post(
            "/sources?kind=webhook", data={"name": "Hook"}, headers=HX
        )
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
    new_secret = re.search(
        r'id="webhook-secret-value"[^>]*>([^<]+)', rotated.text
    ).group(1)
    assert new_secret != shown
    assert "The previous secret no longer works." in rotated.text
    assert client.get(f"/sources/{source_id}").text.find(new_secret) == -1
    assert client.get(f"/api/sources/{source_id}").json()["collection_url"] == before

    old = send_webhook(client, source_id, shown, webhook_payload())
    assert old.status_code == 401
    assert fake.events == {}
    accepted = send_webhook(client, source_id, new_secret, webhook_payload())
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
    plain = webhook_secret(client.app.state.engine, source_id)
    assert plain not in json_rotate.text
    assert plain != new_secret
    assert (
        client.get(f"/api/sources/{source_id}").json()["collection_url"]
        == listed["collection_url"]
    )
    assert len(source_mappings(client.app.state.engine, source_id)) == 1

    removed = client.delete(f"/api/sources/{source_id}?confirm=true", headers=JSON)
    assert removed.status_code == 200
    assert source_mappings(client.app.state.engine, source_id) == []
    with Session(client.app.state.engine) as session:
        assert session.get(SourceSecret, source_id) is None
        assert session.get(SyncState, source_id) is None


def test_sync_page_shows_success_and_error(hub_client, fake_radicale) -> None:
    client, fake = hub_client, fake_radicale
    private = client.post("/api/sources", json={"name": "Home"}, headers=JSON)
    assert private.status_code == 201
    ok_source = create_webhook_source(client, "Ok hook")
    secret = webhook_secret(client.app.state.engine, ok_source["id"])
    assert (
        send_webhook(client, ok_source["id"], secret, webhook_payload()).status_code
        == 200
    )
    bad = create_webhook_source(client, "Bad hook")
    bad_secret = webhook_secret(client.app.state.engine, bad["id"])
    fake.fail_put = True
    failed = send_webhook(client, bad["id"], bad_secret, webhook_payload(id="ext-9"))
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


def test_missing_sync_row_is_filled_for_an_existing_private_source(
    hub_client, fake_radicale
) -> None:
    client = hub_client
    created = client.post("/api/sources", json={"name": "Home"}, headers=JSON).json()
    with Session(client.app.state.engine) as session, session.begin():
        session.execute(delete(SyncState).where(SyncState.source_id == created["id"]))
    ensure_sync_states(client.app.state.engine)
    again = client.get(f"/api/sources/{created['id']}").json()
    assert again["status"] == "na"
    assert again["status_label"] == "not applicable"


def test_a_second_run_does_not_start_while_the_slot_is_held(
    hub_client, fake_radicale
) -> None:
    client = hub_client
    created = create_webhook_source(client)
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
    assert source_state(client.app.state.engine, created["id"])["status"] == "ok"


def test_webhook_stops_at_the_event_limit(
    hub_client, fake_radicale, monkeypatch
) -> None:
    monkeypatch.setattr("app.sync.apply.MAX_EVENTS", 5)
    monkeypatch.setattr("app.sync.webhook.MAX_EVENTS", 5)
    client, fake = hub_client, fake_radicale
    created = create_webhook_source(client, "Full")
    secret = webhook_secret(client.app.state.engine, created["id"])
    assert (
        send_webhook(client, created["id"], secret, webhook_payload()).status_code
        == 200
    )
    success = source_state(client.app.state.engine, created["id"])
    collection = created["collection_path"]
    for number in range(4):
        fake.events[f"{collection}pad-{number}.ics"] = b"x"
    assert fake.count_events(collection) == 5
    puts = len(put_calls(fake))
    refused = send_webhook(
        client, created["id"], secret, webhook_payload(id="extra", summary="Extra")
    )
    assert refused.status_code == 500
    assert refused.json()["detail"] == english(CAPACITY_MESSAGE)
    assert "Traceback" not in refused.text
    failed = source_state(client.app.state.engine, created["id"])
    assert failed["status"] == "error"
    assert failed["last_error_message"] == CAPACITY_MESSAGE
    assert failed["last_success_at"] == success["last_success_at"]
    assert len(put_calls(fake)) == puts
    assert [
        row["external_id"]
        for row in source_mappings(client.app.state.engine, created["id"])
    ] == ["ext-1"]

    updated = send_webhook(
        client, created["id"], secret, webhook_payload(summary="Changed title")
    )
    assert updated.status_code == 200
    assert source_state(client.app.state.engine, created["id"])["status"] == "ok"
    assert b"Changed title" in next(iter(fake.events.values())) or any(
        b"Changed title" in body for body in fake.events.values()
    )
