"""Shared fixtures for the test suite."""

import base64
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from tests.fakes import FakeRadicale

from app.db.models import Base
from app.db.session import make_engine
from app.main import create_app
from app.mcp.runtime import bind as bind_mcp

SECRET_KEY = base64.urlsafe_b64encode(b"calendar-hub-local-dev-key-32b!!").decode()


@pytest.fixture
def fake_radicale() -> FakeRadicale:
    return FakeRadicale()


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = make_engine(tmp_path / "hub.sqlite")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def hub_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_radicale: FakeRadicale
) -> Iterator[TestClient]:
    monkeypatch.setenv("HUB_SQLITE_PATH", str(tmp_path / "hub.sqlite"))
    monkeypatch.setenv("HUB_HOST", "localhost:8443")
    monkeypatch.setenv("HUB_USER", "owner")
    monkeypatch.setenv("HUB_PASSWORD", "secret-password-value")
    monkeypatch.setenv("HUB_INTERNAL_USER", "internal-account-name")
    monkeypatch.setenv("HUB_INTERNAL_PASSWORD", "internal-secret-value")
    monkeypatch.setenv("HUB_SECRET_KEY", SECRET_KEY)
    client = TestClient(create_app())
    client.__enter__()
    client.app.state.scheduler.pause()
    client.app.state.radicale = fake_radicale
    client.app.state.scheduler.radicale = fake_radicale
    bind_mcp(client.app.state.engine, fake_radicale, client.app.state.agent_confirms)
    try:
        yield client
    finally:
        if fake_radicale.block_put is not None:
            fake_radicale.block_put.set()
        client.__exit__(None, None, None)
