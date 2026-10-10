"""Health endpoint and application lifecycle."""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.health import (
    KOPIA_ERROR,
    LITESTREAM_ERROR,
    RADICALE_ERROR,
    report,
    write_heartbeat,
)
from app.db.models import Base
from app.db.session import make_engine
from app.main import create_app


def _silence_backups(monkeypatch) -> None:
    monkeypatch.setenv("HUB_RADICALE_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("HUB_LITESTREAM_STATUS_PATH", raising=False)
    monkeypatch.delenv("HUB_LITESTREAM_HEARTBEAT_PATH", raising=False)
    monkeypatch.delenv("HUB_KOPIA_STATUS_PATH", raising=False)


def test_health_starts_and_stops(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "hub.sqlite"
    monkeypatch.setenv("HUB_SQLITE_PATH", str(db_path))
    _silence_backups(monkeypatch)

    with TestClient(create_app()) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body["sqlite"] == "ok"
        assert body["radicale"] == "error"
        assert body["radicale_error"] == RADICALE_ERROR
        assert body["litestream"] == "error"
        assert body["litestream_at"] is None
        assert body["litestream_error"] == LITESTREAM_ERROR
        assert body["kopia"] == "error"
        assert body["kopia_at"] is None
        assert body["kopia_error"] == KOPIA_ERROR
        assert body["disk_free_bytes"] > 0

    assert db_path.exists()
    engine = make_engine(db_path)
    try:
        with engine.connect() as connection:
            mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
            tables = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).all()
        assert str(mode).lower() == "wal"
        assert {row[0] for row in tables} == {
            "sources",
            "source_secrets",
            "event_mappings",
            "sync_states",
        }
    finally:
        engine.dispose()
    assert set(Base.metadata.tables) == {
        "sources",
        "source_secrets",
        "event_mappings",
        "sync_states",
    }


def _route_paths(routes) -> set[str]:
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
        included = getattr(route, "original_router", None)
        if included is not None:
            paths.update(_route_paths(included.routes))
    return paths


def test_backup_scripts_keep_the_hub_key_out() -> None:
    root = Path(__file__).resolve().parents[1] / "deploy"
    text = "\n".join(
        (root / name).read_text(encoding="utf-8")
        for name in ("litestream-entrypoint.sh", "kopia-entrypoint.sh", "litestream.yml", "kopia.yml")
    )
    assert "HUB_SECRET_KEY" not in text
    assert "public-read" not in text
    assert "kopia server start" not in text.lower()
    assert "restore-if-db-not-exists" not in text


def test_mounted_routes() -> None:
    paths = _route_paths(create_app().routes)
    assert "/api/health" in paths
    assert "/api/sources" in paths
    assert "/api/sources/{source_id}" in paths
    assert "/api/calendars" in paths
    assert "/api/caldav-info" in paths
    assert "/api/webhooks/{source_id}" in paths
    assert "/api/sources/{source_id}/rotate-secret" in paths
    assert "/api/sync" in paths
    assert "/sync" in paths
    assert "/api/sources/{source_id}/sync" in paths
    assert "/api/oauth/google/start" in paths
    assert "/api/oauth/google/callback" in paths
    assert "/health" in paths
    assert "/health/panel" in paths


def test_heartbeat_copies_the_litestream_status_time(tmp_path: Path, monkeypatch) -> None:
    status = tmp_path / "litestream-sync"
    beat = tmp_path / "litestream-heartbeat"
    status.write_text("2026-10-10T11:00:00Z\n", encoding="utf-8")
    monkeypatch.setenv("HUB_LITESTREAM_STATUS_PATH", str(status))
    monkeypatch.setenv("HUB_LITESTREAM_HEARTBEAT_PATH", str(beat))

    write_heartbeat()
    assert beat.read_text(encoding="utf-8") == "2026-10-10T11:00:00Z\n"

    status.write_text("not a time\n", encoding="utf-8")
    write_heartbeat()
    assert beat.read_text(encoding="utf-8") == "2026-10-10T11:00:00Z\n"

    monkeypatch.delenv("HUB_LITESTREAM_STATUS_PATH")
    beat.unlink()
    write_heartbeat()
    assert not beat.exists()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _same_panel(panel: str, page: str) -> None:
    def diskless(html: str) -> str:
        return re.sub(r"(Free disk</th>\s*<td>)\d+", r"\1", html)

    assert diskless(panel.strip()) in diskless(page)


def test_health_ages_are_strict(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "hub.sqlite"
    beat = tmp_path / "litestream-heartbeat"
    kopia = tmp_path / "kopia-backup"
    monkeypatch.setenv("HUB_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("HUB_RADICALE_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("HUB_LITESTREAM_STATUS_PATH", raising=False)
    monkeypatch.setenv("HUB_LITESTREAM_HEARTBEAT_PATH", str(beat))
    monkeypatch.setenv("HUB_KOPIA_STATUS_PATH", str(kopia))
    moment = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
    beat.write_text("2026-10-10T11:55:00Z\n", encoding="utf-8")
    kopia.write_text("2026-10-10T04:00:00Z\n", encoding="utf-8")

    with TestClient(create_app()) as client:
        fresh = report(client.app.state.engine, client.app.state.radicale, now=moment)
        assert fresh["litestream"] == "ok"
        assert fresh["litestream_error"] == ""
        assert fresh["kopia"] == "ok"
        assert fresh["kopia_error"] == ""

        beat.write_text("2026-10-10T11:54:59Z\n", encoding="utf-8")
        kopia.write_text("2026-10-10T03:59:59Z\n", encoding="utf-8")
        stale = report(client.app.state.engine, client.app.state.radicale, now=moment)
        assert stale["litestream"] == "error"
        assert stale["litestream_error"] == LITESTREAM_ERROR
        assert stale["kopia"] == "error"
        assert stale["kopia_error"] == KOPIA_ERROR
        assert stale["radicale"] == "error"
        assert stale["radicale_error"] == RADICALE_ERROR


def test_health_page_shows_fresh_and_stale_backups(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "hub.sqlite"
    beat = tmp_path / "litestream-heartbeat"
    kopia = tmp_path / "kopia-backup"
    monkeypatch.setenv("HUB_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("HUB_RADICALE_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("HUB_LITESTREAM_STATUS_PATH", raising=False)
    monkeypatch.setenv("HUB_LITESTREAM_HEARTBEAT_PATH", str(beat))
    monkeypatch.setenv("HUB_KOPIA_STATUS_PATH", str(kopia))
    now = datetime.now(timezone.utc)
    old_beat = _stamp(now - timedelta(minutes=5, seconds=5))
    old_kopia = _stamp(now - timedelta(hours=8, seconds=5))
    beat.write_text(old_beat + "\n", encoding="utf-8")
    kopia.write_text(old_kopia + "\n", encoding="utf-8")

    with TestClient(create_app()) as client:
        page = client.get("/health")
        panel = client.get("/health/panel")
        assert page.status_code == 200
        assert panel.status_code == 200
        _same_panel(panel.text, page.text)
        assert 'hx-trigger="every 10s"' in panel.text
        assert "SQLite" in page.text
        assert "Radicale" in page.text
        assert "Litestream heartbeat" in page.text
        assert "Backup heartbeat" in page.text
        assert "Free disk" in page.text
        assert RADICALE_ERROR in page.text
        assert LITESTREAM_ERROR in page.text
        assert KOPIA_ERROR in page.text
        assert old_kopia in page.text
        assert "Traceback" not in page.text

        fresh_kopia = _stamp(now - timedelta(minutes=1))
        fresh_beat = _stamp(now - timedelta(seconds=30))
        kopia.write_text(fresh_kopia + "\n", encoding="utf-8")
        beat.write_text(fresh_beat + "\n", encoding="utf-8")
        current = client.get("/health")
        current_panel = client.get("/health/panel")
        assert fresh_kopia in current.text
        assert fresh_beat in current.text
        assert KOPIA_ERROR not in current.text
        assert LITESTREAM_ERROR not in current.text
        assert RADICALE_ERROR in current.text
        _same_panel(current_panel.text, current.text)
