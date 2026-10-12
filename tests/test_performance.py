"""List-page timing, sync start, and a 5,000-event ICS run."""

import math
import threading
import time
from pathlib import Path

import test_ics as ics_tests
from app.sync.runner import wait_for

HX = {"HX-Request": "true"}


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    rank = math.ceil(0.95 * len(ordered))
    return ordered[rank - 1]


def test_list_pages_stay_under_300ms(tmp_path: Path, monkeypatch) -> None:
    client, _fake = ics_tests._open(tmp_path, monkeypatch)
    try:
        for number in range(20):
            created = client.post(
                "/api/sources",
                json={"name": f"Source {number}"},
                headers=ics_tests.JSON,
            )
            assert created.status_code == 201, created.text
        samples: list[float] = []
        for path in ("/sources", "/calendars", "/sync"):
            for _repeat in range(10):
                started = time.perf_counter()
                response = client.get(path)
                samples.append(time.perf_counter() - started)
                assert response.status_code == 200
        assert len(samples) == 30
        assert _p95(samples) < 0.3
    finally:
        ics_tests._close(client)


def test_sync_start_answers_within_a_second(tmp_path: Path, monkeypatch) -> None:
    feed = ics_tests.Feed()
    feed.hold = threading.Event()
    server, base = ics_tests._server(feed)
    client, _fake = ics_tests._open(tmp_path, monkeypatch)
    source_id = ""
    try:
        feed.default = ics_tests._calendar(*ics_tests._event("one"))
        created = ics_tests._create(client, f"{base}/feed.ics", name="Feed")
        source_id = created["id"]
        private = client.post("/api/sources", json={"name": "Home"}, headers=ics_tests.JSON)
        hook = client.post(
            "/api/sources",
            json={"name": "Hook", "kind": "webhook"},
            headers=ics_tests.JSON,
        )
        assert private.status_code == 201
        assert hook.status_code == 201
        started = time.perf_counter()
        first = ics_tests._sync(client, source_id)
        assert time.perf_counter() - started < 1
        assert first.status_code == 202
        deadline = time.monotonic() + 2
        while len(feed.requests) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(feed.requests) == 1
        for _repeat in range(9):
            started = time.perf_counter()
            response = ics_tests._sync(client, source_id)
            assert time.perf_counter() - started < 1
            assert response.status_code == 409
            assert response.json()["error"] == "sync_running"

        started = time.perf_counter()
        panel = client.post(f"/sources/{source_id}/sync?view=sync", headers=HX)
        assert time.perf_counter() - started < 1
        assert panel.status_code == 200
        assert "A sync is already running." in panel.text
        assert ">running<" in panel.text
        assert len(feed.requests) == 1

        private_sync = ics_tests._sync(client, private.json()["id"])
        assert private_sync.status_code == 422
        assert private_sync.json()["error"] == "sync_not_supported"
        hook_sync = ics_tests._sync(client, hook.json()["id"])
        assert hook_sync.status_code == 422
        assert hook_sync.json()["error"] == "sync_not_supported"
    finally:
        if feed.hold is not None:
            feed.hold.set()
        if source_id:
            wait_for(client.app.state.engine, source_id)
        ics_tests._close(client)
        ics_tests._close_server(server)


def test_five_thousand_events_finish_within_ten_minutes(tmp_path: Path, monkeypatch) -> None:
    feed = ics_tests.Feed()
    server, base = ics_tests._server(feed)
    client, fake = ics_tests._open(tmp_path, monkeypatch)
    try:
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0"]
        for number in range(5000):
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:e{number}",
                    "DTSTAMP:20261001T080000Z",
                    "DTSTART;TZID=Europe/Amsterdam:20261007T090015",
                    "DTEND;TZID=Europe/Amsterdam:20261007T100015",
                    f"SUMMARY:e{number}",
                    "END:VEVENT",
                ]
            )
        lines.append("END:VCALENDAR")
        feed.default = ("\r\n".join(lines) + "\r\n").encode()
        created = ics_tests._create(client, f"{base}/feed.ics", name="Large")
        app_id = id(client.app)
        started = time.perf_counter()
        assert ics_tests._sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"], timeout=600) == "ok"
        assert time.perf_counter() - started < 600
        assert id(client.app) == app_id
        assert len(fake.events) == 5000
        state = ics_tests._state(client.app.state.engine, created["id"])
        assert state["events_seen"] == 5000
        assert state["events_upserted"] == 5000
    finally:
        ics_tests._close(client)
        ics_tests._close_server(server)
