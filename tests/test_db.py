from __future__ import annotations

import types

from brigade.db import SCHEMA_LOCK_ID, ensure_schema


class _FakeCursor:
    def __init__(self, calls: list[tuple[str, tuple[object, ...]]]) -> None:
        self.calls = calls
        self.rows: list[tuple[str]] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows


class _FakeConnection:
    def __init__(self, calls: list[tuple[str, tuple[object, ...]]]) -> None:
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self):
        return _FakeCursor(self.calls)


def test_ensure_schema_wraps_execution_in_advisory_lock(monkeypatch, tmp_path):
    calls: list[tuple[str, tuple[object, ...]]] = []
    psycopg = types.SimpleNamespace(
        connect=lambda dsn, autocommit=True: _FakeConnection(calls)
    )
    monkeypatch.setitem(__import__("sys").modules, "psycopg", psycopg)
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001.sql").write_text("create table if not exists test_table (id int);")

    ensure_schema("postgresql://unused", root=migrations)

    assert calls[0] == ("select pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
    assert calls[1][0].startswith("create table if not exists brigade_schema_migrations")
    assert calls[2][0].startswith("create table if not exists brigade_schema_migration_failures")
    assert calls[3][0] == "select id from brigade_schema_migrations"
    assert calls[4][0] == "create table if not exists test_table (id int);"
    assert calls[5] == ("insert into brigade_schema_migrations (id) values (%s)", ("0001",))
    assert calls[6] == ("delete from brigade_schema_migration_failures where id = %s", ("0001",))
    assert calls[7] == ("select pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,))
