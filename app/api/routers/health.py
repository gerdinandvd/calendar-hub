"""SQLite, Radicale, backup heartbeats, and free disk."""

import asyncio
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from sqlalchemy.engine import Engine

from app.db.session import sqlite_path
from app.radicale.client import RadicaleClient, RadicaleError

logger = logging.getLogger("app")

router = APIRouter()

STAMP = "%Y-%m-%dT%H:%M:%SZ"
HEARTBEAT_SECONDS = 60
LITESTREAM_MAX_AGE = timedelta(minutes=5)
KOPIA_MAX_AGE = timedelta(hours=8)

RADICALE_ERROR = "Radicale is not reachable."
LITESTREAM_ERROR = "The Litestream heartbeat is older than 5 minutes."
KOPIA_ERROR = "The backup is older than 8 hours."


def report(
    engine: Engine,
    radicale: RadicaleClient,
    now: datetime | None = None,
) -> dict[str, object]:
    """One reading of the health section. HTTP stays 200 when a part is unhealthy."""

    moment = _utc(now)
    sqlite_status = _sqlite_status(engine)
    radicale_ok = _radicale_ok(radicale)
    litestream_at, litestream_ok = _stamp_status(
        "HUB_LITESTREAM_HEARTBEAT_PATH",
        moment,
        LITESTREAM_MAX_AGE,
    )
    kopia_at, kopia_ok = _stamp_status("HUB_KOPIA_STATUS_PATH", moment, KOPIA_MAX_AGE)
    return {
        "sqlite": sqlite_status,
        "radicale": "ok" if radicale_ok else "error",
        "radicale_error": "" if radicale_ok else RADICALE_ERROR,
        "litestream": "ok" if litestream_ok else "error",
        "litestream_at": litestream_at,
        "litestream_error": "" if litestream_ok else LITESTREAM_ERROR,
        "kopia": "ok" if kopia_ok else "error",
        "kopia_at": kopia_at,
        "kopia_error": "" if kopia_ok else KOPIA_ERROR,
        "disk_free_bytes": _disk_free(),
    }


def write_heartbeat() -> None:
    """Copy the Litestream status time into the heartbeat file. Never writes the current time."""

    status_name = os.environ.get("HUB_LITESTREAM_STATUS_PATH", "").strip()
    beat_name = os.environ.get("HUB_LITESTREAM_HEARTBEAT_PATH", "").strip()
    if not status_name or not beat_name:
        return
    stamp = read_stamp(Path(status_name))
    if stamp is None:
        return
    path = Path(beat_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(stamp + "\n", encoding="utf-8")
    temporary.replace(path)


def read_stamp(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    line = text.strip().splitlines()[0] if text.strip() else ""
    parsed = _parse(line)
    if parsed is None:
        return None
    return parsed.strftime(STAMP)


class Heartbeat:
    """Writes the Litestream heartbeat once a minute while the process runs."""

    def __init__(self, interval: float = HEARTBEAT_SECONDS) -> None:
        self.interval = interval
        self._loop_stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def shutdown(self) -> None:
        self._loop_stop.set()
        if self._task is not None:
            await self._task

    async def _loop(self) -> None:
        while not self._loop_stop.is_set():
            try:
                write_heartbeat()
            except Exception:
                logger.error("heartbeat_error")
            try:
                await asyncio.wait_for(self._loop_stop.wait(), timeout=self.interval)
            except TimeoutError:
                continue


@router.get("/api/health")
def health_endpoint(request: Request) -> dict[str, object]:
    return report(request.app.state.engine, request.app.state.radicale)


def _sqlite_status(engine: Engine) -> str:
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
    except Exception:
        logger.error("sqlite_error")
        return "error"
    return "ok"


def _radicale_ok(radicale: RadicaleClient) -> bool:
    try:
        return radicale.reachable()
    except RadicaleError:
        return False


def _stamp_status(env_name: str, now: datetime, limit: timedelta) -> tuple[str | None, bool]:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None, False
    stamp = read_stamp(Path(raw))
    if stamp is None:
        return None, False
    parsed = _parse(stamp)
    if parsed is None or now - parsed > limit:
        return stamp, False
    return stamp, True


def _disk_free() -> int:
    try:
        return int(shutil.disk_usage(sqlite_path().parent).free)
    except OSError:
        logger.error("disk_error")
        return 0


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp.strip(), STAMP).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)
