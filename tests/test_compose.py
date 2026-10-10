"""Cold start and the phase 1 access boundary."""

import base64
import http.client
import json
import re
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SERVICES = {"hub", "radicale", "caddy", "litestream", "backup"}
HTTPS_PORT = 8443


def _compose(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _parse_ps(stdout: str) -> list[dict]:
    text = stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _basic(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _https(
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, bytes, bool]:
    context = ssl._create_unverified_context()
    connection = http.client.HTTPSConnection(
        "localhost",
        HTTPS_PORT,
        context=context,
        timeout=15,
    )
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        secure = isinstance(connection.sock, ssl.SSLSocket)
        return response.status, payload, secure
    finally:
        connection.close()


def _published_ports(item: dict) -> list[int]:
    publishers = item.get("Publishers") or item.get("publishers") or []
    ports: list[int] = []
    for publisher in publishers:
        port = publisher.get("PublishedPort", publisher.get("publishedPort", 0)) or 0
        if port:
            ports.append(int(port))
    return ports


@pytest.fixture(scope="module")
def stack() -> Iterator[dict[str, str]]:
    env_path = ROOT / ".env"
    created_env = False
    if not env_path.exists():
        env_path.write_text(
            (ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        created_env = True
    up = _compose(ROOT, "up", "-d", "--build", "--wait")
    try:
        if up.returncode != 0:
            logs = _compose(ROOT, "logs", "--no-color")
            raise AssertionError(
                up.stdout + "\n" + up.stderr + "\n" + logs.stdout + "\n" + logs.stderr
            )
        yield _load_env(env_path)
    finally:
        _compose(ROOT, "down", "--volumes", "--remove-orphans")
        if created_env:
            env_path.unlink(missing_ok=True)


@pytest.mark.integration
def test_stack_cold_start(stack: dict[str, str]) -> None:
    del stack
    ps = _compose(ROOT, "ps", "--format", "json")
    assert ps.returncode == 0, ps.stderr
    services = {item["Service"]: item for item in _parse_ps(ps.stdout)}

    assert set(services) == EXPECTED_SERVICES
    for name, item in services.items():
        state = item.get("State") or item.get("state")
        health = item.get("Health") or item.get("health")
        assert state == "running", name
        assert health == "healthy", name
        ports = _published_ports(item)
        if name == "caddy":
            assert set(ports) == {HTTPS_PORT}, item
        else:
            assert ports == [], item

    config = _compose(ROOT, "config", "--format", "json")
    assert config.returncode == 0, config.stderr
    hub_volumes = json.loads(config.stdout)["services"]["hub"].get("volumes") or []
    assert all("radicale" not in json.dumps(volume) for volume in hub_volumes)

    probe = socket.socket()
    probe.settimeout(2)
    try:
        assert probe.connect_ex(("127.0.0.1", 5232)) != 0
    finally:
        probe.close()


@pytest.mark.integration
def test_public_access_requires_owner_and_blocks_internal_account(stack: dict[str, str]) -> None:
    owner = _basic(stack["HUB_USER"], stack["HUB_PASSWORD"])
    internal = _basic(stack["HUB_INTERNAL_USER"], stack["HUB_INTERNAL_PASSWORD"])

    status, body, secure = _https("GET", "/")
    assert secure
    assert status == 401
    assert b"Traceback" not in body

    status, _, _ = _https("GET", "/api/sources")
    assert status == 401

    status, _, _ = _https("PROPFIND", "/radicale/", {"Depth": "0"})
    assert status == 401

    status, propfind_body, _ = _https(
        "PROPFIND",
        "/radicale/",
        {"Depth": "0", "Authorization": owner},
    )
    assert status == 207, propfind_body[:300]

    status, _, _ = _https(
        "PROPFIND",
        "/radicale/",
        {"Depth": "0", "Authorization": internal},
    )
    assert status == 401

    marker = "phase1-not-written"
    status, _, _ = _https(
        "PUT",
        f"/radicale/{quote(stack['HUB_USER'])}/{marker}.ics",
        {"Content-Type": "text/calendar"},
        b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
    )
    assert status == 401
    status, read_back, _ = _https(
        "GET",
        f"/radicale/{quote(stack['HUB_USER'])}/{marker}.ics",
        {"Authorization": owner},
    )
    assert status != 200
    assert marker.encode() not in read_back

    status, _, _ = _https("GET", "/api/webhooks/example")
    assert status != 401
    status, _, _ = _https("GET", "/api/oauth/google/callback")
    assert status != 401

    users = _compose(ROOT, "exec", "-T", "radicale", "cat", "/data/users")
    assert users.returncode == 0, users.stderr
    assert stack["HUB_PASSWORD"] not in users.stdout
    assert stack["HUB_INTERNAL_PASSWORD"] not in users.stdout
    assert users.stdout.count("$2") == 2

    internal_direct = _compose(
        ROOT,
        "exec",
        "-T",
        "radicale",
        "curl",
        "-s",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "-u",
        f"{stack['HUB_INTERNAL_USER']}:{stack['HUB_INTERNAL_PASSWORD']}",
        "-X",
        "PROPFIND",
        "-H",
        "Depth: 0",
        "http://127.0.0.1:5232/",
    )
    assert internal_direct.returncode == 0, internal_direct.stderr
    assert internal_direct.stdout.strip() == "207"


def _json_call(
    method: str,
    path: str,
    auth: str,
    body: dict | None = None,
) -> tuple[int, dict]:
    payload = None if body is None else json.dumps(body).encode()
    status, raw, _ = _https(
        method,
        path,
        {"Authorization": auth, "Content-Type": "application/json"},
        payload,
    )
    parsed = json.loads(raw) if raw else {}
    return status, parsed


def _propfind(path: str, auth: str) -> tuple[int, bytes]:
    body = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<D:propfind xmlns:D='DAV:' xmlns:A='http://apple.com/ns/ical/'>"
        "<D:prop><D:displayname/><A:calendar-color/></D:prop>"
        "</D:propfind>"
    ).encode()
    status, payload, _ = _https(
        "PROPFIND",
        path,
        {
            "Authorization": auth,
            "Depth": "0",
            "Content-Type": "application/xml",
        },
        body,
    )
    return status, payload


@pytest.mark.integration
def test_private_collection_roundtrip(stack: dict[str, str]) -> None:
    owner = _basic(stack["HUB_USER"], stack["HUB_PASSWORD"])
    status, created = _json_call("POST", "/api/sources", owner, {"name": "Home"})
    assert status == 201, created
    source_id = created["id"]
    assert created["collection_path"] == f"/{quote(stack['HUB_USER'])}/{source_id}/"
    collection = urlparse(created["collection_url"]).path
    assert collection.endswith(created["collection_path"])
    assert stack["HUB_INTERNAL_USER"] not in created["collection_url"]
    assert "password" not in created

    status, props = _propfind(collection, owner)
    assert status == 207, props[:300]
    assert b"Home" in props
    assert b"#1a73e8" in props

    event = collection + "event-1.ics"
    calendar = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//calendar-hub//EN\r\n"
        "BEGIN:VEVENT\r\nUID:event-1\r\nDTSTAMP:20261008T060000Z\r\n"
        "DTSTART:20261008T090000Z\r\nDTEND:20261008T100000Z\r\n"
        "SUMMARY:First title\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()
    status, _, _ = _https(
        "PUT",
        event,
        {"Authorization": owner, "Content-Type": "text/calendar"},
        calendar,
    )
    assert status in {201, 204}, status
    status, stored, _ = _https("GET", event, {"Authorization": owner})
    assert status == 200
    assert b"SUMMARY:First title" in stored

    status, _, _ = _https(
        "PUT",
        event,
        {"Authorization": owner, "Content-Type": "text/calendar"},
        b"this is not a calendar\r\n",
    )
    assert status >= 400
    status, stored, _ = _https("GET", event, {"Authorization": owner})
    assert status == 200
    assert b"SUMMARY:First title" in stored
    assert b"UID:event-1" in stored

    updated = calendar.replace(b"First title", b"Second title")
    status, _, _ = _https(
        "PUT",
        event,
        {"Authorization": owner, "Content-Type": "text/calendar"},
        updated,
    )
    assert status in {200, 201, 204}, status

    status, renamed = _json_call(
        "PATCH",
        f"/api/sources/{source_id}",
        owner,
        {"name": "Home renamed"},
    )
    assert status == 200, renamed
    assert renamed["collection_url"] == created["collection_url"]
    status, props = _propfind(collection, owner)
    assert status == 207, props[:300]
    assert b"Home renamed" in props
    assert b"#1a73e8" in props
    status, stored, _ = _https("GET", event, {"Authorization": owner})
    assert b"SUMMARY:Second title" in stored
    assert b"UID:event-1" in stored

    status, other = _json_call("POST", "/api/sources", owner, {"name": "Other"})
    assert status == 201, other
    status, stored, _ = _https("GET", event, {"Authorization": owner})
    assert b"SUMMARY:Second title" in stored

    status, disabled = _json_call(
        "PATCH",
        f"/api/sources/{source_id}",
        owner,
        {"enabled": 0},
    )
    assert status == 200, disabled
    assert disabled["enabled"] == 0
    status, props = _propfind(collection, owner)
    assert status == 207, props[:300]

    status, _, _ = _https(
        "DELETE",
        event,
        {"Authorization": owner},
    )
    assert status in {200, 204}
    status, gone, _ = _https("GET", event, {"Authorization": owner})
    assert status != 200
    assert b"Second title" not in gone

    status, refused = _json_call("DELETE", f"/api/sources/{source_id}", owner, {})
    assert status == 422, refused
    status, still = _json_call("GET", f"/api/sources/{source_id}", owner)
    assert status == 200
    assert still["name"] == "Home renamed"

    status, removed = _json_call(
        "DELETE",
        f"/api/sources/{source_id}?confirm=true",
        owner,
        {},
    )
    assert status == 200, removed
    status, missing = _json_call("GET", f"/api/sources/{source_id}", owner)
    assert status == 404
    assert missing["error"] == "not_found"
    status, props = _propfind(collection, owner)
    assert status != 207


def _exchange(
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    context = ssl._create_unverified_context()
    connection = http.client.HTTPSConnection(
        "localhost",
        HTTPS_PORT,
        context=context,
        timeout=15,
    )
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        found = {name.lower(): value for name, value in response.getheaders()}
        return response.status, found, payload
    finally:
        connection.close()


@pytest.mark.integration
def test_webhook_upsert_is_readable_via_caldav(stack: dict[str, str]) -> None:
    owner = _basic(stack["HUB_USER"], stack["HUB_PASSWORD"])
    status, headers, raw = _exchange(
        "POST",
        "/sources?kind=webhook",
        {
            "Authorization": owner,
            "HX-Request": "true",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        b"name=Webhook+feed",
    )
    assert status == 200, raw[:300]
    assert headers.get("cache-control") == "no-store"
    secret_match = re.search(br'id="webhook-secret-value"[^>]*>([^<]+)', raw)
    url_match = re.search(br'id="webhook-url">([^<]+)', raw)
    assert secret_match is not None
    assert url_match is not None
    secret = secret_match.group(1).decode()
    source_id = url_match.group(1).decode().rstrip("/").rsplit("/", 1)[-1]
    status, page, _ = _https("GET", "/sources", {"Authorization": owner})
    assert status == 200
    assert secret.encode() not in page

    payload = {
        "id": "ext-1",
        "action": "upsert",
        "summary": "Webhook title",
        "start": "2026-10-07T09:00:15",
        "end": "2026-10-07T10:00:15",
        "timezone": "Europe/Amsterdam",
    }
    started = time.monotonic()
    status, created, _ = _https(
        "POST",
        f"/api/webhooks/{source_id}",
        {"X-Webhook-Secret": secret, "Content-Type": "application/json"},
        json.dumps(payload).encode(),
    )
    assert status == 200, created
    status, listed = _json_call("GET", "/api/sources", owner)
    assert status == 200
    source = next(item for item in listed["sources"] if item["id"] == source_id)
    collection = urlparse(source["collection_url"]).path
    list_body = (
        "<?xml version='1.0' encoding='utf-8'?>"
        "<D:propfind xmlns:D='DAV:'><D:prop><D:getetag/></D:prop></D:propfind>"
    ).encode()
    status, listing, _ = _https(
        "PROPFIND",
        collection,
        {"Authorization": owner, "Depth": "1", "Content-Type": "application/xml"},
        list_body,
    )
    assert status == 207, listing[:300]
    hrefs = re.findall(br"href>([^<]+\.ics)", listing, flags=re.IGNORECASE)
    assert hrefs
    stored = b""
    for href in hrefs:
        path = href.decode()
        if "://" in path:
            path = urlparse(path).path
        if not path.startswith("/"):
            path = collection + path
        status, stored, _ = _https("GET", path, {"Authorization": owner})
        if status == 200 and b"Webhook title" in stored:
            break
    else:
        raise AssertionError(listing[:500])
    assert time.monotonic() - started < 5
    assert b"DTSTART;TZID=Europe/Amsterdam:20261007T090015" in stored
    assert b"DTEND;TZID=Europe/Amsterdam:20261007T100015" in stored
    assert b"SUMMARY:Webhook title" in stored


class _FeedHandler(BaseHTTPRequestHandler):
    body = b""

    def do_GET(self) -> None:
        payload = _FeedHandler.body
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar")
        self.send_header("ETag", '"bench"')
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


def _feed_server(body: bytes) -> tuple[ThreadingHTTPServer, str]:
    _FeedHandler.body = body
    server = ThreadingHTTPServer(("0.0.0.0", 0), _FeedHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    return server, f"http://host.docker.internal:{port}/feed.ics"


def _calendar(count: int) -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0"]
    for number in range(count):
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
    return ("\r\n".join(lines) + "\r\n").encode()


def _make_room(owner: str, needed: int) -> None:
    sources = _sources(owner)
    extras = [item for item in sources if str(item["name"]).startswith("Bench ")]
    while len(sources) + needed > 20 and extras:
        victim = extras.pop()
        status, body = _json_call(
            "DELETE",
            f"/api/sources/{victim['id']}?confirm=true",
            owner,
            {},
        )
        assert status == 200, body
        sources = _sources(owner)
    assert len(sources) + needed <= 20, [item["name"] for item in sources]


def _sources(owner: str) -> list[dict]:
    status, body = _json_call("GET", "/api/sources", owner)
    assert status == 200, body
    return body["sources"]


def _sync_row(owner: str, source_id: str) -> dict:
    status, body = _json_call("GET", "/api/sync", owner)
    assert status == 200, body
    return next(item for item in body["sources"] if item["id"] == source_id)


def _wait_sync(owner: str, source_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    row = _sync_row(owner, source_id)
    while row["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.5)
        row = _sync_row(owner, source_id)
    return row


def _start_sync(owner: str, source_id: str) -> None:
    status, body = _json_call("POST", f"/api/sources/{source_id}/sync", owner, {})
    assert status in {202, 409}, body


def _memory_bytes(text: str) -> int:
    match = re.match(r"([0-9]+(?:[.,][0-9]+)?)\s*([A-Za-z]+)", text.strip())
    assert match is not None, text
    number = float(match.group(1).replace(",", "."))
    unit = match.group(2).lower()
    scale = {
        "b": 1,
        "kib": 1024,
        "kb": 1000,
        "mib": 1024**2,
        "mb": 1000**2,
        "gib": 1024**3,
        "gb": 1000**3,
    }
    assert unit in scale, text
    return int(number * scale[unit])


def _sample_hub(container: str) -> tuple[int, float]:
    stats = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}\t{{.CPUPerc}}", container],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert stats.returncode == 0, stats.stderr
    line = stats.stdout.strip().splitlines()[-1]
    memory, cpu = line.split("\t")
    assert cpu.endswith("%"), cpu
    return _memory_bytes(memory.split("/")[0]), float(cpu[:-1].strip())


def _event_bytes(uid: str, summary: str) -> bytes:
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//calendar-hub//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTAMP:20261008T060000Z\r\n"
        "DTSTART:20261008T090000Z\r\nDTEND:20261008T100000Z\r\n"
        f"SUMMARY:{summary}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return text.encode()


def _put_event(path: str, owner: str, body: bytes) -> None:
    status, _, _ = _https(
        "PUT",
        path,
        {"Authorization": owner, "Content-Type": "text/calendar"},
        body,
    )
    assert status in {200, 201, 204}, status


def _get_event(path: str, owner: str) -> tuple[str, bytes]:
    status, headers, payload = _exchange("GET", path, {"Authorization": owner})
    assert status == 200, payload[:200]
    return headers.get("etag", ""), payload


@pytest.mark.integration
def test_five_thousand_events_stay_within_memory_and_cpu(stack: dict[str, str]) -> None:
    owner = _basic(stack["HUB_USER"], stack["HUB_PASSWORD"])
    have = len(_sources(owner))
    assert have <= 19, have
    for number in range(19 - have):
        status, created = _json_call("POST", "/api/sources", owner, {"name": f"Bench {number}"})
        assert status == 201, created
    server, url = _feed_server(_calendar(5000))
    try:
        status, created = _json_call(
            "POST",
            "/api/sources",
            owner,
            {
                "name": "Large feed",
                "kind": "ics",
                "ics_url": url,
                "ics_allow_http": 1,
                "poll_seconds": 86400,
            },
        )
        assert status == 201, created
        assert len(_sources(owner)) == 20
        container = _compose(ROOT, "ps", "-q", "hub").stdout.strip()
        assert container
        peaks = {"memory": 0, "cpu": 0.0, "samples": 0}
        stop = threading.Event()

        def sample() -> None:
            while not stop.is_set():
                try:
                    memory, cpu = _sample_hub(container)
                except (AssertionError, OSError):
                    stop.wait(15)
                    continue
                peaks["memory"] = max(peaks["memory"], memory)
                peaks["cpu"] = max(peaks["cpu"], cpu)
                peaks["samples"] += 1
                stop.wait(15)

        sampler = threading.Thread(target=sample)
        sampler.start()
        try:
            _start_sync(owner, created["id"])
            row = _wait_sync(owner, created["id"], 600)
        finally:
            stop.set()
            sampler.join(30)
        assert row["status"] == "ok", {
            "status": row["status"],
            "message": row.get("last_error_message"),
            "seen": row.get("events_seen"),
            "upserted": row.get("events_upserted"),
        }
        assert row["events_seen"] == 5000
        assert row["events_upserted"] == 5000
        assert peaks["samples"] > 0
        peak_memory = peaks["memory"]
        peak_cpu = peaks["cpu"]
        assert peak_memory < 512 * 1024 * 1024, peak_memory
        assert peak_cpu < 100, peak_cpu
        collection = urlparse(created["collection_url"]).path
        status, stored, _ = _https("GET", f"{collection}e0.ics", {"Authorization": owner})
        assert status == 200
        assert b"UID:e0" in stored
        assert b"SUMMARY:e0" in stored
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.integration
def test_etag_stays_stable_across_twenty_propfinds(stack: dict[str, str]) -> None:
    owner = _basic(stack["HUB_USER"], stack["HUB_PASSWORD"])
    _make_room(owner, 2)
    status, private = _json_call("POST", "/api/sources", owner, {"name": "Quiet private"})
    assert status == 201, private
    collection = urlparse(private["collection_url"]).path
    event = f"{collection}quiet-1.ics"
    _put_event(event, owner, _event_bytes("quiet-1", "Quiet title"))
    server, url = _feed_server(_calendar(1))
    try:
        status, ics = _json_call(
            "POST",
            "/api/sources",
            owner,
            {
                "name": "Quiet feed",
                "kind": "ics",
                "ics_url": url,
                "ics_allow_http": 1,
                "poll_seconds": 86400,
            },
        )
        assert status == 201, ics
        _start_sync(owner, ics["id"])
        synced = _wait_sync(owner, ics["id"], 60)
        assert synced["status"] == "ok", synced
        before = {item["id"]: item.get("poll_seconds") for item in _sources(owner)}
        etag, body = _get_event(event, owner)
        assert b"UID:quiet-1" in body
        assert b"SUMMARY:Quiet title" in body
        minute_etag, minute_body = etag, body
        for number in range(20):
            status, payload = _propfind(collection, owner)
            assert status == 207, payload[:300]
            if number < 19:
                time.sleep(60)
            if number == 0:
                minute_etag, minute_body = _get_event(event, owner)
        later_etag, later_body = _get_event(event, owner)
        assert later_etag == etag == minute_etag
        assert later_body == body == minute_body
        after = {item["id"]: item.get("poll_seconds") for item in _sources(owner)}
        assert after == before
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.integration
def test_caldav_survives_hub_and_radicale_restarts(stack: dict[str, str]) -> None:
    owner = _basic(stack["HUB_USER"], stack["HUB_PASSWORD"])
    _make_room(owner, 1)
    status, private = _json_call("POST", "/api/sources", owner, {"name": "Restart private"})
    assert status == 201, private
    collection = urlparse(private["collection_url"]).path
    event = f"{collection}restart-1.ics"
    _put_event(event, owner, _event_bytes("restart-1", "Restart title"))
    etag, body = _get_event(event, owner)
    assert b"UID:restart-1" in body
    assert b"SUMMARY:Restart title" in body

    outcome: dict[str, subprocess.CompletedProcess[str]] = {}

    def restart_hub() -> None:
        outcome["hub"] = _compose(ROOT, "restart", "hub")

    worker = threading.Thread(target=restart_hub)
    worker.start()
    saw_propfind = False
    saw_event = False
    deadline = time.monotonic() + 90
    while worker.is_alive() and time.monotonic() < deadline:
        try:
            status, _payload = _propfind(collection, owner)
            read_status, stored, _ = _https("GET", event, {"Authorization": owner})
        except (TimeoutError, OSError, http.client.HTTPException):
            time.sleep(0.2)
            continue
        if status == 207:
            saw_propfind = True
        if read_status == 200 and stored == body:
            saw_event = True
        time.sleep(0.2)
    worker.join(30)
    assert outcome["hub"].returncode == 0, outcome["hub"].stderr
    assert saw_propfind
    assert saw_event
    kept_etag, kept = _get_event(event, owner)
    assert kept == body
    assert kept_etag == etag

    restarted = _compose(ROOT, "restart", "radicale")
    assert restarted.returncode == 0, restarted.stderr
    found = 0
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            found, _payload = _propfind(collection, owner)
        except (TimeoutError, OSError, http.client.HTTPException):
            found = 0
        if found == 207:
            break
        time.sleep(1)
    assert found == 207
    _restored_etag, restored = _get_event(event, owner)
    assert b"UID:restart-1" in restored
    assert b"SUMMARY:Restart title" in restored
