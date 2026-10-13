"""The collection client sends CalDAV writes and does not touch event bodies."""

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.radicale.client import RadicaleClient, RadicaleError


class _Handler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, bytes]] = []
    propstat = "200"

    def _read(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _store(self) -> bytes:
        body = self._read()
        self.calls.append((self.command, self.path, body))
        return body

    def do_MKCOL(self) -> None:
        self._store()
        self.send_response(201)
        self.end_headers()

    def do_MKCALENDAR(self) -> None:
        self._store()
        self.send_response(201)
        self.end_headers()

    def do_PROPPATCH(self) -> None:
        self._store()
        payload = (
            "<?xml version='1.0'?>"
            "<D:multistatus xmlns:D='DAV:'><D:response><D:propstat>"
            f"<D:status>HTTP/1.1 {self.propstat} OK</D:status>"
            "</D:propstat></D:response></D:multistatus>"
        ).encode()
        self.send_response(207)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_DELETE(self) -> None:
        self._store()
        self.send_response(204)
        self.end_headers()

    def do_PUT(self) -> None:
        self._store()
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


def _server() -> tuple[ThreadingHTTPServer, str]:
    _Handler.calls = []
    _Handler.propstat = "200"
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}"


def test_collection_writes_do_not_put_events() -> None:
    server, base = _server()
    try:
        client = RadicaleClient(base, "hub-internal", "secret")
        client.create_collection("/owner/one/", "A & B", "#1a73e8")
        client.update_properties("/owner/one/", "A & B", "#1a73e8")
        client.delete_collection("/owner/one/")
        methods = [call[0] for call in _Handler.calls]
        assert methods == ["MKCOL", "MKCALENDAR", "PROPPATCH", "PROPPATCH", "DELETE"]
        proppatch = _Handler.calls[2][2]
        assert b"A &amp; B" in proppatch
        assert b"#1a73e8" in proppatch
        assert b"calendar-color" in proppatch
        assert "secret" not in str(_Handler.calls)
    finally:
        server.shutdown()
        server.server_close()


def test_failed_proppatch_removes_the_new_collection() -> None:
    server, base = _server()
    _Handler.propstat = "403"
    try:
        client = RadicaleClient(base, "hub-internal", "secret")
        try:
            client.create_collection("/owner/one/", "Home", "#1a73e8")
            raised = False
        except RadicaleError:
            raised = True
        assert raised
        assert [call[0] for call in _Handler.calls] == [
            "MKCOL",
            "MKCALENDAR",
            "PROPPATCH",
            "DELETE",
        ]
    finally:
        server.shutdown()
        server.server_close()


class _EventHandler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str | None, str | None]] = []
    put_statuses = [204]
    get_status = 200
    etags = ['"v1"']

    def do_GET(self) -> None:
        self.calls.append(("GET", None, None))
        body = b"existing"
        etag = self.etags.pop(0) if self.etags else '"v1"'
        self.send_response(self.get_status)
        if self.get_status == 200:
            self.send_header("ETag", etag)
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.get_status == 200:
            self.wfile.write(body)

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        match = self.headers.get("If-Match")
        content_type = self.headers.get("Content-Type")
        self.calls.append(("PUT", match, content_type))
        status = self.put_statuses.pop(0) if self.put_statuses else 204
        self.send_response(status)
        self.end_headers()

    def do_DELETE(self) -> None:
        self.calls.append(("DELETE", None, None))
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


def _event_server() -> tuple[ThreadingHTTPServer, str]:
    _EventHandler.calls = []
    _EventHandler.put_statuses = [204]
    _EventHandler.get_status = 200
    _EventHandler.etags = ['"v1"']
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EventHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}"


def test_put_retries_once_after_etag_conflict(caplog) -> None:
    server, base = _event_server()
    _EventHandler.put_statuses = [412, 204]
    _EventHandler.etags = ['"v1"', '"v2"']
    try:
        client = RadicaleClient(base, "hub-internal", "secret")
        with caplog.at_level("ERROR", logger="app"):
            client.put_event(
                "/owner/one/event.ics", b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
            )
        methods = [call[0] for call in _EventHandler.calls]
        assert methods == ["GET", "PUT", "GET", "PUT"]
        assert _EventHandler.calls[1][1] == '"v1"'
        assert _EventHandler.calls[3][1] == '"v2"'
        assert _EventHandler.calls[1][2].startswith("text/calendar")
        assert "status=412" not in caplog.text
    finally:
        server.shutdown()
        server.server_close()


def test_failed_etag_retry_raises_and_logs_status(caplog) -> None:
    server, base = _event_server()
    _EventHandler.put_statuses = [412, 500]
    try:
        client = RadicaleClient(base, "hub-internal", "secret")
        with caplog.at_level("ERROR", logger="app"):
            try:
                client.put_event(
                    "/owner/one/event.ics", b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
                )
                raised = False
            except RadicaleError:
                raised = True
        assert raised
        assert [call[0] for call in _EventHandler.calls] == ["GET", "PUT", "GET", "PUT"]
        assert "radicale_error method=PUT status=500" in caplog.text
    finally:
        server.shutdown()
        server.server_close()


def test_count_events_counts_ics_children_only() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_PROPFIND(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
            assert self.headers.get("Depth") == "1"
            body = (
                b"<?xml version='1.0' encoding='utf-8'?>"
                b"<D:multistatus xmlns:D='DAV:'>"
                b"<D:response><D:href>/owner/one/</D:href></D:response>"
                b"<D:response><D:href>/owner/one/keep.ics</D:href></D:response>"
                b"<D:response><D:href>/owner/one/keep.ics</D:href></D:response>"
                b"<D:response><D:href>http://radicale:5232/owner/one/other.ics</D:href></D:response>"
                b"<D:response><D:href>/owner/one/notes.txt</D:href></D:response>"
                b"<D:response><D:href>/owner/two/skip.ics</D:href></D:response>"
                b"</D:multistatus>"
            )
            self.send_response(207)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        client = RadicaleClient(f"http://127.0.0.1:{port}", "hub-internal", "secret")
        assert client.count_events("/owner/one/") == 2
    finally:
        server.shutdown()
        server.server_close()


def test_count_events_logs_no_status_when_the_service_is_down(caplog) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    client = RadicaleClient(f"http://127.0.0.1:{port}", "hub-internal", "secret")
    with caplog.at_level("ERROR", logger="app"):
        try:
            client.count_events("/owner/one/")
            raised = False
        except RadicaleError:
            raised = True
    assert raised
    assert "radicale_error method=PROPFIND status=none" in caplog.text
    assert "Traceback" not in caplog.text


def test_delete_event_accepts_a_missing_resource() -> None:
    server, base = _event_server()
    try:
        client = RadicaleClient(base, "hub-internal", "secret")
        client.delete_event("/owner/one/missing.ics")
        assert _EventHandler.calls[-1][0] == "DELETE"
    finally:
        server.shutdown()
        server.server_close()
