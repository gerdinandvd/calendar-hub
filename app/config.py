"""Frozen Settings loaded once at boot. No other app.* imports."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_SQLITE = "hub.sqlite"
_DEFAULT_RADICALE_URL = "http://radicale:5232"
_DEFAULT_ICS_TIMEOUT = 30.0


@dataclass(frozen=True)
class Settings:
    """Immutable process configuration."""

    sqlite_path: Path
    host: str
    user: str
    radicale_url: str
    radicale_internal_user: str
    radicale_internal_password: str
    secret_key: str
    ics_timeout: float
    litestream_status_path: str
    litestream_heartbeat_path: str
    kopia_status_path: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ
        return cls(
            sqlite_path=Path(env.get("HUB_SQLITE_PATH", _DEFAULT_SQLITE)),
            host=env.get("HUB_HOST", "").strip(),
            user=env.get("HUB_USER", "").strip(),
            radicale_url=env.get("HUB_RADICALE_URL", _DEFAULT_RADICALE_URL).rstrip("/"),
            radicale_internal_user=env.get("HUB_INTERNAL_USER", ""),
            radicale_internal_password=env.get("HUB_INTERNAL_PASSWORD", ""),
            secret_key=env.get("HUB_SECRET_KEY", "").strip(),
            ics_timeout=_positive_float(
                env.get("HUB_ICS_TIMEOUT", ""), _DEFAULT_ICS_TIMEOUT
            ),
            litestream_status_path=env.get("HUB_LITESTREAM_STATUS_PATH", "").strip(),
            litestream_heartbeat_path=env.get(
                "HUB_LITESTREAM_HEARTBEAT_PATH", ""
            ).strip(),
            kopia_status_path=env.get("HUB_KOPIA_STATUS_PATH", "").strip(),
        )


def _positive_float(raw: str, default: float) -> float:
    text = raw.strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value
