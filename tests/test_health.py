"""Health endpoint and application lifecycle."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.db.models import Base
from app.db.session import make_engine
from app.main import create_app


def test_health_starts_and_stops(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "hub.sqlite"
    monkeypatch.setenv("HUB_SQLITE_PATH", str(db_path))

    with TestClient(create_app()) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

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
        assert tables == []
    finally:
        engine.dispose()
    assert Base.metadata.tables == {}


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


def test_only_health_is_mounted() -> None:
    paths = _route_paths(create_app().routes)
    assert "/api/health" in paths
    assert "/api/sources" not in paths
    assert "/api/webhooks/{source_id}" not in paths
