"""Recovery exercise on a copy. Uses a local backup stand-in, never production B2."""

import base64
import http.client
import json
import re
import sqlite3
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "calendar-hub-restore"
PORT = 8444
RESTORE_LIMIT_SECONDS = 4 * 60 * 60


def _compose(env_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT, "--env-file", str(env_path), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _write_env(path: Path) -> None:
    overrides = {
        "HUB_HTTPS_PORT": str(PORT),
        "HUB_ENV_FILE": path.resolve().as_posix(),
        "LITESTREAM_REPLICA_URL": "file:///replica/hub",
        "KOPIA_REPOSITORY": "filesystem",
        "KOPIA_REPO_PATH": "/repo",
        "KOPIA_PASSWORD": "restore-exercise-only",
    }
    lines: list[str] = []
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(line)
            continue
        key = stripped.split("=", 1)[0]
        if key not in overrides:
            lines.append(line)
    for key, value in overrides.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _env_map(service: dict) -> dict[str, str]:
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {str(key): "" if value is None else str(value) for key, value in raw.items()}
    found: dict[str, str] = {}
    for item in raw:
        text = str(item)
        key, value = text.split("=", 1)
        found[key] = value
    return found


def _basic(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _https(
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    context = ssl._create_unverified_context()
    connection = http.client.HTTPSConnection("localhost", PORT, context=context, timeout=20)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        found = {name.lower(): value for name, value in response.getheaders()}
        return response.status, found, payload
    finally:
        connection.close()


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value
    return values


def _assert_local_only(env_path: Path) -> None:
    config = _compose(env_path, "config", "--format", "json")
    assert config.returncode == 0, config.stderr
    parsed = json.loads(config.stdout)
    litestream = _env_map(parsed["services"]["litestream"])
    backup = _env_map(parsed["services"]["backup"])
    assert litestream["LITESTREAM_REPLICA_URL"] == "file:///replica/hub"
    assert backup["KOPIA_REPOSITORY"] == "filesystem"
    assert "s3://" not in config.stdout
    assert "backblazeb2" not in config.stdout
    hub_volumes = json.dumps(parsed["services"]["hub"].get("volumes") or [])
    assert "radicale-data" not in hub_volumes


def _wait(predicate, timeout: float, detail: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(2)
    raise AssertionError(detail)


def _replica_has(env_path: Path, names: tuple[str, ...], dest: Path) -> bool:
    restored = _compose(
        env_path,
        "exec",
        "-T",
        "litestream",
        "sh",
        "-c",
        'rm -f /tmp/check.sqlite && litestream restore -o /tmp/check.sqlite "$LITESTREAM_REPLICA_URL"',
    )
    if restored.returncode != 0:
        _replica_has.detail = (restored.stdout + "\n" + restored.stderr)[-800:]
        return False
    container = _compose(env_path, "ps", "-q", "litestream")
    cid = container.stdout.strip()
    if container.returncode != 0 or not cid:
        _replica_has.detail = "litestream container id missing"
        return False
    copied = subprocess.run(
        ["docker", "cp", f"{cid}:/tmp/check.sqlite", str(dest)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if copied.returncode != 0 or not dest.exists():
        _replica_has.detail = (copied.stdout + "\n" + copied.stderr)[-800:]
        return False
    connection = sqlite3.connect(dest)
    try:
        found = {
            row[0]
            for row in connection.execute("select name from sources").fetchall()
        }
    except sqlite3.Error as exc:
        _replica_has.detail = str(exc)
        return False
    finally:
        connection.close()
    if not set(names).issubset(found):
        _replica_has.detail = f"found {sorted(found)}"
        return False
    return True


def _key_absent(env_path: Path, service: str, path: str, key: str) -> None:
    scanned = _compose(
        env_path,
        "exec",
        "-T",
        service,
        "sh",
        "-c",
        'grep -R -a -F -q -- "$1" "$2"; status=$?; if [ "$status" -eq 0 ]; then echo FOUND; elif [ "$status" -eq 1 ]; then echo ABSENT; else echo ERROR; exit 1; fi',
        "sh",
        key,
        path,
    )
    assert scanned.returncode == 0, scanned.stderr
    assert scanned.stdout.strip() == "ABSENT"


def _docker_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=None,
    )


@pytest.mark.integration
def test_restore_exercise_brings_back_source_and_event(tmp_path: Path) -> None:
    env_path = tmp_path / "restore.env"
    _write_env(env_path)
    _assert_local_only(env_path)
    values = _load_env(env_path)
    owner = _basic(values["HUB_USER"], values["HUB_PASSWORD"])
    up = _compose(env_path, "up", "-d", "--build", "--wait")
    try:
        if up.returncode != 0:
            logs = _compose(env_path, "logs", "--no-color")
            raise AssertionError(up.stdout + "\n" + up.stderr + "\n" + logs.stdout + "\n" + logs.stderr)

        def kopia_ready() -> bool:
            status = _compose(env_path, "exec", "-T", "backup", "cat", "/status/kopia-backup")
            return status.returncode == 0 and bool(status.stdout.strip())

        _wait(kopia_ready, 180, "kopia did not write a status file")

        status, _headers, raw = _https(
            "POST",
            "/sources?kind=webhook",
            {
                "Authorization": owner,
                "HX-Request": "true",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            b"name=Restore+hook",
        )
        assert status == 200, raw[:300]
        secret_match = re.search(br'id="webhook-secret-value"[^>]*>([^<]+)', raw)
        url_match = re.search(br'id="webhook-url">([^<]+)', raw)
        assert secret_match is not None
        assert url_match is not None
        secret = secret_match.group(1).decode()
        source_id = url_match.group(1).decode().rstrip("/").rsplit("/", 1)[-1]

        status, _headers, private = _https(
            "POST",
            "/api/sources",
            {"Authorization": owner, "Content-Type": "application/json"},
            json.dumps({"name": "Restore private"}).encode(),
        )
        assert status == 201, private
        created = json.loads(private)
        collection = urlparse(created["collection_url"]).path
        if not collection.endswith("/"):
            collection = f"{collection}/"
        event = f"{collection}restore-1.ics"
        calendar = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//calendar-hub//EN\r\n"
            "BEGIN:VEVENT\r\nUID:restore-1\r\nDTSTAMP:20261010T120000Z\r\n"
            "DTSTART:20261010T090000Z\r\nDTEND:20261010T100000Z\r\n"
            "SUMMARY:Restore title\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        ).encode()
        status, _, stored = _https(
            "PUT",
            event,
            {"Authorization": owner, "Content-Type": "text/calendar"},
            calendar,
        )
        assert status in {201, 204}, stored[:300]

        check_db = tmp_path / "check.sqlite"
        try:
            _wait(
                lambda: _replica_has(env_path, ("Restore private", "Restore hook"), check_db),
                120,
                "litestream replica did not contain the source rows",
            )
        except AssertionError as exc:
            logs = _compose(env_path, "logs", "--no-color", "--tail", "60", "litestream")
            detail = getattr(_replica_has, "detail", "")
            raise AssertionError(f"{exc}\n{detail}\n{logs.stdout[-2000:]}") from None
        snapshot = _compose(env_path, "exec", "-T", "backup", "/bin/kopia", "snapshot", "create", "/source")
        assert snapshot.returncode == 0, snapshot.stdout + "\n" + snapshot.stderr

        _key_absent(env_path, "litestream", "/replica", values["HUB_SECRET_KEY"])
        _key_absent(env_path, "backup", "/repo", values["HUB_SECRET_KEY"])
        _key_absent(env_path, "hub", "/status", values["HUB_SECRET_KEY"])

        started = time.monotonic()
        stopped = _compose(env_path, "stop")
        assert stopped.returncode == 0, stopped.stderr
        sqlite_restore = _docker_run(
            [
                "run",
                "--rm",
                "--env-file",
                str(env_path),
                "-v",
                f"{PROJECT}_hub-data:/data",
                "-v",
                f"{PROJECT}_litestream-replica:/replica",
                "-v",
                f"{(ROOT / 'deploy' / 'litestream-entrypoint.sh').resolve().as_posix()}:/usr/local/bin/calendar-hub-litestream.sh:ro",
                "--entrypoint",
                "/bin/sh",
                "litestream/litestream:0.5.17",
                "/usr/local/bin/calendar-hub-litestream.sh",
                "restore",
            ],
        )
        assert sqlite_restore.returncode == 0, sqlite_restore.stdout + "\n" + sqlite_restore.stderr
        radicale_restore = _docker_run(
            [
                "run",
                "--rm",
                "--hostname",
                "backup",
                "--env-file",
                str(env_path),
                "-e",
                "KOPIA_CACHE_DIRECTORY=/cache",
                "-e",
                "KOPIA_CONFIG_PATH=/cache/kopia.config",
                "-e",
                "KOPIA_LOG_DIR=/cache/log",
                "-v",
                f"{PROJECT}_radicale-data:/restore",
                "-v",
                f"{PROJECT}_kopia-repo:/repo",
                "-v",
                f"{PROJECT}_kopia-cache:/cache",
                "-v",
                f"{(ROOT / 'deploy' / 'kopia-entrypoint.sh').resolve().as_posix()}:/usr/local/bin/calendar-hub-kopia.sh:ro",
                "--entrypoint",
                "/bin/sh",
                "kopia/kopia:0.23.1",
                "/usr/local/bin/calendar-hub-kopia.sh",
                "restore",
            ],
        )
        assert radicale_restore.returncode == 0, radicale_restore.stdout + "\n" + radicale_restore.stderr
        again = _compose(env_path, "up", "-d", "--wait")
        if again.returncode != 0:
            logs = _compose(env_path, "logs", "--no-color")
            raise AssertionError(again.stdout + "\n" + again.stderr + "\n" + logs.stdout + "\n" + logs.stderr)

        status, _headers, listed = _https("GET", "/api/sources", {"Authorization": owner})
        assert status == 200, listed
        names = {item["name"] for item in json.loads(listed)["sources"]}
        assert "Restore private" in names
        assert "Restore hook" in names
        status, _, event_body = _https("GET", event, {"Authorization": owner})
        assert status == 200, event_body[:300]
        assert b"UID:restore-1" in event_body
        assert b"SUMMARY:Restore title" in event_body
        status, _, hook = _https(
            "POST",
            f"/api/webhooks/{source_id}",
            {"X-Webhook-Secret": secret, "Content-Type": "application/json"},
            json.dumps(
                {
                    "id": "restore-hook-1",
                    "action": "upsert",
                    "summary": "Hook after restore",
                    "start": "2026-10-10T11:00:00",
                    "end": "2026-10-10T12:00:00",
                    "timezone": "Europe/Amsterdam",
                }
            ).encode(),
        )
        assert status == 200, hook[:300]
        elapsed = time.monotonic() - started
        assert elapsed < RESTORE_LIMIT_SECONDS, elapsed
        print(f"restore-seconds={elapsed:.1f}")
    finally:
        _compose(env_path, "down", "--volumes", "--remove-orphans")
