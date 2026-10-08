"""Fake source and temporary database, still without import behavior."""

from urllib.request import urlopen

from sqlalchemy import inspect
from sqlalchemy.engine import Engine


def test_fake_http_server_answers(fake_http_server) -> None:
    host, port = fake_http_server.server_address
    with urlopen(f"http://{host}:{port}/", timeout=5) as response:
        assert response.status == 200
        assert response.read() == b"ok"


def test_temporary_sqlite_is_wal_without_tables(sqlite_engine: Engine) -> None:
    with sqlite_engine.connect() as connection:
        mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        table_names = inspect(sqlite_engine).get_table_names()
    assert str(mode).lower() == "wal"
    assert table_names == []
