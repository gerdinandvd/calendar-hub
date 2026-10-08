"""The collection client sends CalDAV writes and does not touch event bodies."""

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
