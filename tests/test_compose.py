"""Koude start en de toegangsgrens van fase 1."""

import base64
import http.client
import json
import socket
import ssl
import subprocess
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

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

    marker = "fase1-niet-geschreven"
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

    status, _, _ = _https("GET", "/api/webhooks/voorbeeld")
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
