"""SQLite engine in WAL mode, without a schema."""

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine


def sqlite_path() -> Path:
    return Path(os.environ.get("HUB_SQLITE_PATH", "hub.sqlite"))


def make_engine(path: Path | None = None) -> Engine:
    db_path = sqlite_path() if path is None else path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _wal(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine
