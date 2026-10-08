"""Cold start and the phase 1 access boundary."""

import base64
import http.client
import json
import socket
import ssl
import subprocess
from collections.abc import Iterator
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
