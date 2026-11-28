"""Settings loading from environment."""

from pathlib import Path

from app.config import Settings


def test_settings_from_env_defaults() -> None:
    settings = Settings.from_env({})
    assert settings.sqlite_path == Path("hub.sqlite")
    assert settings.host == ""
    assert settings.user == ""
    assert settings.radicale_url == "http://radicale:5232"
    assert settings.radicale_internal_user == ""
    assert settings.radicale_internal_password == ""
    assert settings.secret_key == ""
    assert settings.ics_timeout == 30.0
    assert settings.litestream_status_path == ""
    assert settings.litestream_heartbeat_path == ""
    assert settings.kopia_status_path == ""


def test_settings_from_env_reads_values() -> None:
    settings = Settings.from_env(
        {
            "HUB_SQLITE_PATH": "/data/hub.sqlite",
            "HUB_HOST": "example.test",
            "HUB_USER": "owner",
            "HUB_RADICALE_URL": "http://radicale:5232/",
            "HUB_INTERNAL_USER": "hub",
            "HUB_INTERNAL_PASSWORD": "secret",
            "HUB_SECRET_KEY": "key-value",
            "HUB_ICS_TIMEOUT": "12.5",
            "HUB_LITESTREAM_STATUS_PATH": "/run/litestream.status",
            "HUB_LITESTREAM_HEARTBEAT_PATH": "/run/litestream.beat",
            "HUB_KOPIA_STATUS_PATH": "/run/kopia.status",
        }
    )
    assert settings.sqlite_path == Path("/data/hub.sqlite")
    assert settings.host == "example.test"
    assert settings.user == "owner"
    assert settings.radicale_url == "http://radicale:5232"
    assert settings.radicale_internal_user == "hub"
    assert settings.radicale_internal_password == "secret"
    assert settings.secret_key == "key-value"
    assert settings.ics_timeout == 12.5
    assert settings.litestream_status_path == "/run/litestream.status"
    assert settings.litestream_heartbeat_path == "/run/litestream.beat"
    assert settings.kopia_status_path == "/run/kopia.status"


def test_settings_ics_timeout_rejects_non_positive() -> None:
    assert Settings.from_env({"HUB_ICS_TIMEOUT": "0"}).ics_timeout == 30.0
    assert Settings.from_env({"HUB_ICS_TIMEOUT": "-1"}).ics_timeout == 30.0
    assert Settings.from_env({"HUB_ICS_TIMEOUT": "nope"}).ics_timeout == 30.0


def test_settings_is_frozen() -> None:
    settings = Settings.from_env({})
    try:
        settings.host = "changed"  # type: ignore[misc]
    except Exception as exc:
        assert isinstance(exc, (AttributeError, TypeError))
    else:
        raise AssertionError("Settings must be frozen")


def test_create_app_stores_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HUB_SQLITE_PATH", str(tmp_path / "hub.sqlite"))
    monkeypatch.setenv("HUB_HOST", "localhost:8443")
    monkeypatch.setenv("HUB_USER", "owner")
    monkeypatch.setenv("HUB_INTERNAL_USER", "internal")
    monkeypatch.setenv("HUB_INTERNAL_PASSWORD", "internal-secret")
    monkeypatch.setenv(
        "HUB_SECRET_KEY",
        "Y2FsZW5kYXItaHViLWxvY2FsLWRldi1rZXktMzJiISE=",
    )
    from fastapi.testclient import TestClient

    from app.main import create_app

    client = TestClient(create_app())
    client.__enter__()
    try:
        assert client.app.state.settings.user == "owner"
        assert client.app.state.settings.sqlite_path == tmp_path / "hub.sqlite"
        assert client.app.state.settings.radicale_internal_user == "internal"
    finally:
        client.__exit__(None, None, None)
