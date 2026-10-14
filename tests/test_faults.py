"""Timeouts, parse errors, a stopped calendar, process stop, and a failed ETag retry."""

import logging
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import test_ics as ics_tests
import test_webhook as webhook_tests
from app.i18n import english
from app.radicale.client import RadicaleClient
from app.sync.apply import SAVE_MESSAGE
from app.sync.ics import TIMEOUT_MESSAGE
from app.sync.runner import PROCESS_STOPPED, stop_runs, wait_for


class _Store:
    def __init__(self, events: dict[str, bytes], accept: int) -> None:
        self.events = dict(events)
        self.accept = accept
        self.puts = 0
        self.retried = False
        self.deletes: list[str] = []


def _handler(store: _Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_PROPFIND(self) -> None:
            self._read()
            rows = "".join(
                f"<D:response><D:href>{path}</D:href></D:response>"
                for path in store.events
            )
            body = (
                "<?xml version='1.0' encoding='utf-8'?>"
                f"<D:multistatus xmlns:D='DAV:'>{rows}</D:multistatus>"
            ).encode()
            self._send(207, body)

        def do_GET(self) -> None:
            body = store.events.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("ETag", '"v1"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self) -> None:
            payload = self._read()
            if store.puts >= store.accept:
                if not store.retried:
                    store.retried = True
                    self.send_response(412)
                    self.end_headers()
                    return
                self.send_response(500)
                self.end_headers()
                return
            store.puts += 1
            store.events[self.path] = payload
            self.send_response(204)
            self.end_headers()

        def do_DELETE(self) -> None:
            self._read()
            store.deletes.append(self.path)
            self.send_response(204)
            self.end_headers()

        def _read(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or "0")
            return self.rfile.read(length) if length else b""

        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _serve(store: _Store) -> tuple[ThreadingHTTPServer, RadicaleClient]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(store))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    client = RadicaleClient(
        f"http://127.0.0.1:{port}", "internal-account-name", "secret"
    )
    return server, client


def _close_server(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def _closed_client() -> RadicaleClient:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return RadicaleClient(f"http://127.0.0.1:{port}", "internal-account-name", "secret")


def _swap(client, radicale: RadicaleClient) -> None:
    client.app.state.radicale = radicale
    scheduler = getattr(client.app.state, "scheduler", None)
    if scheduler is not None:
        scheduler.radicale = radicale


def _sync_row(client, source_id: str) -> dict:
    found = client.get("/api/sync").json()["sources"]
    return next(item for item in found if item["id"] == source_id)


def _assert_visible(client, source_id: str, name: str, message: str, caplog) -> None:
    row = _sync_row(client, source_id)
    assert row["name"] == name
    assert row["last_error_message"] == message
    assert row["last_error_at"]
    page = client.get("/sync")
    assert page.status_code == 200
    assert name in page.text
    assert english(message) in page.text
    assert row["last_error_at"] in page.text
    assert "Traceback" not in page.text
    assert "Traceback" not in caplog.text
    assert f"source_id={source_id}" in caplog.text
    assert "run_id=" in caplog.text


def test_ics_timeout_does_not_fail_another_source(tmp_path: Path, monkeypatch) -> None:
    feed = ics_tests.Feed()
    server, base = ics_tests._server(feed)
    client, fake = ics_tests._open(tmp_path, monkeypatch)
    try:
        feed.bodies["/ok.ics"] = ics_tests._calendar(*ics_tests._event("keep"))
        steady = ics_tests._create(client, f"{base}/ok.ics", name="Steady")
        assert ics_tests._sync(client, steady["id"]).status_code == 202
        assert wait_for(client.app.state.engine, steady["id"]) == "ok"
        before = ics_tests._state(client.app.state.engine, steady["id"])
        synced = ics_tests._mappings(client.app.state.engine, steady["id"])[0][
            "last_synced_at"
        ]
        slow = ics_tests._create(client, f"{base}/slow.ics", name="Slow")
        feed.hold = threading.Event()
        monkeypatch.setenv("HUB_ICS_TIMEOUT", "0.2")
        assert ics_tests._sync(client, slow["id"]).status_code == 202
        assert wait_for(client.app.state.engine, slow["id"], timeout=3) == "error"
        failed = ics_tests._state(client.app.state.engine, slow["id"])
        assert failed["last_error_message"] == TIMEOUT_MESSAGE
        assert failed["last_success_at"] is None
        assert ics_tests._mappings(client.app.state.engine, slow["id"]) == []
        steady_after = ics_tests._state(client.app.state.engine, steady["id"])
        assert steady_after["status"] == "ok"
        assert steady_after["last_success_at"] == before["last_success_at"]
        assert (
            ics_tests._mappings(client.app.state.engine, steady["id"])[0][
                "last_synced_at"
            ]
            == synced
        )
        assert _sync_row(client, slow["id"])["status"] == "error"
        assert _sync_row(client, steady["id"])["status"] == "ok"
        assert len(fake.events) == 1
    finally:
        if feed.hold is not None:
            feed.hold.set()
        ics_tests._close(client)
        ics_tests._close_server(server)


def test_stopped_radicale_on_ics_is_a_sentence_without_a_status(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    feed = ics_tests.Feed()
    server, base = ics_tests._server(feed)
    client, fake = ics_tests._open(tmp_path, monkeypatch)
    try:
        feed.default = ics_tests._calendar(*ics_tests._event("keep"))
        created = ics_tests._create(client, f"{base}/feed.ics", name="Down feed")
        assert ics_tests._sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        before = ics_tests._state(client.app.state.engine, created["id"])
        synced = ics_tests._mappings(client.app.state.engine, created["id"])[0][
            "last_synced_at"
        ]
        saved = dict(fake.events)
        _swap(client, _closed_client())
        feed.etag = '"again"'
        feed.default = ics_tests._calendar(
            *ics_tests._event("keep"), *ics_tests._event("next")
        )
        with caplog.at_level(logging.INFO, logger="app"):
            assert ics_tests._sync(client, created["id"]).status_code == 202
            assert wait_for(client.app.state.engine, created["id"]) == "error"
        failed = ics_tests._state(client.app.state.engine, created["id"])
        assert failed["last_error_message"] == SAVE_MESSAGE
        assert "status=" not in failed["last_error_message"]
        assert failed["cursor"] == before["cursor"]
        assert failed["last_success_at"] == before["last_success_at"]
        assert (
            ics_tests._mappings(client.app.state.engine, created["id"])[0][
                "last_synced_at"
            ]
            == synced
        )
        assert fake.events == saved
        assert "radicale_error method=PROPFIND status=none" in caplog.text
        assert re.search(r"status=\d{3}", caplog.text) is None
        _assert_visible(client, created["id"], "Down feed", SAVE_MESSAGE, caplog)
    finally:
        ics_tests._close(client)
        ics_tests._close_server(server)


def test_stopped_radicale_on_webhook_keeps_the_event(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    client, fake = webhook_tests._open(tmp_path, monkeypatch)
    try:
        created = webhook_tests._webhook(client, "Hook")
        secret = webhook_tests._plain_secret(client.app.state.engine, created["id"])
        assert (
            webhook_tests._send(
                client, created["id"], secret, webhook_tests._payload()
            ).status_code
            == 200
        )
        success = webhook_tests._state(client.app.state.engine, created["id"])
        synced = webhook_tests._mappings(client.app.state.engine, created["id"])[0][
            "last_synced_at"
        ]
        saved = dict(fake.events)
        _swap(client, _closed_client())
        with caplog.at_level(logging.INFO, logger="app"):
            refused = webhook_tests._send(
                client,
                created["id"],
                secret,
                webhook_tests._payload(id="extra", summary="Extra"),
            )
        assert refused.status_code == 500
        assert refused.json()["detail"] == english(SAVE_MESSAGE)
        assert "Traceback" not in refused.text
        failed = webhook_tests._state(client.app.state.engine, created["id"])
        assert failed["last_error_message"] == SAVE_MESSAGE
        assert failed["last_success_at"] == success["last_success_at"]
        rows = webhook_tests._mappings(client.app.state.engine, created["id"])
        assert [row["external_id"] for row in rows] == ["ext-1"]
        assert rows[0]["last_synced_at"] == synced
        assert fake.events == saved
        assert "status=none" in caplog.text
        assert re.search(r"status=\d{3}", caplog.text) is None
    finally:
        webhook_tests._close(client)


def test_webhook_process_stop_keeps_the_event(tmp_path: Path, monkeypatch) -> None:
    client, fake = webhook_tests._open(tmp_path, monkeypatch)
    try:
        created = webhook_tests._webhook(client, "Hook")
        secret = webhook_tests._plain_secret(client.app.state.engine, created["id"])
        assert (
            webhook_tests._send(
                client, created["id"], secret, webhook_tests._payload()
            ).status_code
            == 200
        )
        synced = webhook_tests._mappings(client.app.state.engine, created["id"])[0][
            "last_synced_at"
        ]
        kept = dict(fake.events)
        gate = threading.Event()
        fake.block_put = gate
        box: dict = {}

        def send() -> None:
            box["response"] = webhook_tests._send(
                client,
                created["id"],
                secret,
                webhook_tests._payload(id="extra", summary="Extra"),
            )

        thread = threading.Thread(target=send)
        thread.start()
        assert fake.put_started.wait(2)
        stop_runs(client.app.state.engine, timeout=0.05)
        gate.set()
        thread.join(2)
        assert box["response"].status_code == 500
        assert box["response"].json()["detail"] == english(PROCESS_STOPPED)
        for path, body in kept.items():
            assert fake.events[path] == body
        assert (
            webhook_tests._mappings(client.app.state.engine, created["id"])[0][
                "last_synced_at"
            ]
            == synced
        )
    finally:
        if fake.block_put is not None:
            fake.block_put.set()
        webhook_tests._close(client)


def test_failed_etag_retry_on_ics_keeps_the_first_write(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    feed = ics_tests.Feed()
    server, base = ics_tests._server(feed)
    client, fake = ics_tests._open(tmp_path, monkeypatch)
    caldav = None
    try:
        feed.default = ics_tests._calendar(*ics_tests._event("keep"))
        created = ics_tests._create(client, f"{base}/feed.ics", name="Feed")
        assert ics_tests._sync(client, created["id"]).status_code == 202
        assert wait_for(client.app.state.engine, created["id"]) == "ok"
        before = ics_tests._state(client.app.state.engine, created["id"])
        synced = ics_tests._mappings(client.app.state.engine, created["id"])[0][
            "last_synced_at"
        ]
        store = _Store(fake.events, accept=1)
        caldav, radicale = _serve(store)
        _swap(client, radicale)
        feed.etag = '"next"'
        feed.default = ics_tests._calendar(
            *ics_tests._event("added"), *ics_tests._event("blocked")
        )
        with caplog.at_level(logging.INFO, logger="app"):
            assert ics_tests._sync(client, created["id"]).status_code == 202
            assert wait_for(client.app.state.engine, created["id"]) == "error"
        failed = ics_tests._state(client.app.state.engine, created["id"])
        assert failed["last_error_message"] == SAVE_MESSAGE
        assert failed["cursor"] == before["cursor"]
        assert failed["last_success_at"] == before["last_success_at"]
        rows = {
            row["external_id"]: row
            for row in ics_tests._mappings(client.app.state.engine, created["id"])
        }
        assert rows["keep"]["last_synced_at"] == synced
        assert "added" in rows
        assert "blocked" not in rows
        assert store.deletes == []
        assert any(path.endswith("/added.ics") for path in store.events)
        assert not any(path.endswith("/blocked.ics") for path in store.events)
        assert "radicale_error method=PUT status=500" in caplog.text
        assert "Traceback" not in caplog.text
        assert f"source_id={created['id']}" in caplog.text
        assert "run_id=" in caplog.text
    finally:
        if caldav is not None:
            _close_server(caldav)
        ics_tests._close(client)
        ics_tests._close_server(server)


def test_failed_etag_retry_on_webhook_keeps_the_event(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    client, fake = webhook_tests._open(tmp_path, monkeypatch)
    caldav = None
    try:
        created = webhook_tests._webhook(client, "Hook")
        secret = webhook_tests._plain_secret(client.app.state.engine, created["id"])
        assert (
            webhook_tests._send(
                client, created["id"], secret, webhook_tests._payload()
            ).status_code
            == 200
        )
        success = webhook_tests._state(client.app.state.engine, created["id"])
        synced = webhook_tests._mappings(client.app.state.engine, created["id"])[0][
            "last_synced_at"
        ]
        store = _Store(fake.events, accept=0)
        caldav, radicale = _serve(store)
        _swap(client, radicale)
        with caplog.at_level(logging.INFO, logger="app"):
            refused = webhook_tests._send(
                client,
                created["id"],
                secret,
                webhook_tests._payload(id="extra", summary="Extra"),
            )
        assert refused.status_code == 500
        assert refused.json()["detail"] == english(SAVE_MESSAGE)
        assert "Traceback" not in refused.text
        failed = webhook_tests._state(client.app.state.engine, created["id"])
        assert failed["last_success_at"] == success["last_success_at"]
        rows = webhook_tests._mappings(client.app.state.engine, created["id"])
        assert [row["external_id"] for row in rows] == ["ext-1"]
        assert rows[0]["last_synced_at"] == synced
        assert store.puts == 0
        assert store.deletes == []
        assert "radicale_error method=PUT status=500" in caplog.text
        assert "Traceback" not in caplog.text
    finally:
        if caldav is not None:
            _close_server(caldav)
        webhook_tests._close(client)
