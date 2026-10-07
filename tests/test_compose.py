"""Koude start van de Compose-stack."""

import json
import subprocess
from pathlib import Path
from urllib.request import urlopen

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SERVICES = {"hub", "radicale", "caddy", "litestream", "backup"}


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


@pytest.mark.integration
def test_stack_cold_start() -> None:
    env_path = ROOT / ".env"
    created_env = False
    if not env_path.exists():
        env_path.write_text(
            (ROOT / ".env.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        created_env = True
    try:
        up = _compose(ROOT, "up", "-d", "--build", "--wait")
        assert up.returncode == 0, up.stdout + "\n" + up.stderr

        ps = _compose(ROOT, "ps", "--format", "json")
        assert ps.returncode == 0, ps.stderr
        services = {item["Service"]: item for item in _parse_ps(ps.stdout)}

        assert set(services) == EXPECTED_SERVICES
        for name, item in services.items():
            state = item.get("State") or item.get("state")
            assert state == "running", name
        health = services["hub"].get("Health") or services["hub"].get("health")
        assert health == "healthy"

        with urlopen("http://127.0.0.1:8080/", timeout=10) as response:
            body = response.read().decode("utf-8")
            assert response.status == 200
            assert "calendar-hub skeleton" in body
    finally:
        _compose(ROOT, "down", "--volumes", "--remove-orphans")
        if created_env:
            env_path.unlink(missing_ok=True)
