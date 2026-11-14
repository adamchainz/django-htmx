"""Snapshots on every backend, and scratch databases on PostgreSQL.

The PostgreSQL tests use the server given by CLEANMIG_TEST_POSTGRES_HOST
(default 127.0.0.1), _PORT (5432), _USER and _PASSWORD (rw), and skip when
it can't be reached.
"""

from __future__ import annotations

import os
import threading
from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
from django.conf import settings
from django.db import connections

from django_clean_migrations import scratch as scratch_module
from django_clean_migrations import snapshot as snapshot_module
from django_clean_migrations.scratch import (
    ScratchDatabaseError,
    drop_leftover,
    list_leftovers,
    scratch_database,
)
from django_clean_migrations.snapshot import Mask, Snapshot, _value, take_snapshot
from tests.project import Project
from tests.test_clean import ITEM

# Comparing snapshots


def snap(**kwargs: Any) -> Snapshot:
    return Snapshot("sqlite", **kwargs)


def test_column_differences_named() -> None:
    a = snap(schema={("columns", "t", "t"): [("a", "int"), ("b", "int")]})
    b = snap(schema={("columns", "t", "t"): [("a", "int"), ("b", "text")]})
    (diff,) = a.differences(b)
    assert diff.columns == ("b",)
    assert str(diff) == "schema of table t differs (columns t changed: b)"
    moved = snap(schema={("columns", "t", "t"): [("b", "int"), ("a", "int")]})
    assert a.differences(moved)[0].columns == ("a", "b")
    assert a.differences(b, Mask(schema={("columns", "t", "t")})) == []


def test_without_migrations() -> None:
    a = snap()
    assert a.without_migrations([("shop", "0001")]) is a
    b = snap(data={"django_migrations": Counter({("'shop'", "'0001'"): 1})})
    assert b.without_migrations([("shop", "0001")]).data["django_migrations"] == {}


def test_nondeterminism() -> None:
    a = snap(
        schema={("index", "t", "i"): "x"},
        data={
            "only_a": Counter({("1",): 1}),
            "renamed": Counter({("1",): 1}),
            "both": Counter({("1", "x"): 1, ("2", "y"): 1}),
            "column": Counter({("1", "x"): 1}),
        },
        columns={"renamed": ("a",), "both": ("a", "b"), "column": ("a", "b")},
    )
    b = snap(
        schema={("index", "t", "i"): "y"},
        data={
            "renamed": Counter({("2",): 1}),
            # A row more: leaving the differing columns out doesn't help.
            "both": Counter({("1", "y"): 1, ("3", "x"): 1, ("4", "z"): 1}),
            "column": Counter({("1", "y"): 1}),
        },
        columns={"renamed": ("b",), "both": ("a", "b"), "column": ("a", "b")},
    )
    mask = a.nondeterminism(b)
    assert mask.schema == {("index", "t", "i")}
    assert mask.tables == {"only_a", "renamed", "both"}
    assert mask.columns == {"column": {"b"}}
    assert mask.describe() == (
        "data of both, data of only_a, data of renamed, column.b, index i"
    )


# SQLite, MySQL and other backends


def test_sqlite_objects(django_db_blocker: Any) -> None:
    with django_db_blocker.unblock(), scratch_database() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE t (a integer CHECK (a > 0), b integer,"
                " c text UNIQUE, d datetime, e blob,"
                " CONSTRAINT b_positive CHECK (b > 0))"
            )
            cursor.execute(
                "INSERT INTO t (a, b, c, d, e) VALUES"
                " (1, 1, 'x', '2026-01-01 00:00:00', x'00ff')"
            )
            cursor.execute("CREATE VIEW v AS SELECT a FROM t")
            cursor.execute("CREATE TRIGGER tr AFTER INSERT ON t BEGIN SELECT 1; END")
        snapshot = take_snapshot(connection)
    kinds = {key[0] for key in snapshot.schema}
    assert {"check", "view", "trigger"} <= kinds
    assert ("check", "t", "b_positive:['b']") in snapshot.schema
    # Dates are masked (they differ between runs), bytes compared as bytes.
    assert snapshot.columns["t"] == ("a", "b", "c", "d", "e")
    assert list(snapshot.data["t"]) == [("1", "1", "'x'", "<masked>", "b'\\x00\\xff'")]
    assert (
        snap(data={"t": Counter({("1",): 1})}).differences(snap(), ignore_data=["t"])
        == []
    )


def test_generic_backend(
    django_db_blocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with django_db_blocker.unblock(), scratch_database() as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t (a integer UNIQUE)")
            cursor.execute("CREATE VIEW v AS SELECT a FROM t")
        monkeypatch.setattr(connection, "vendor", "other")
        snapshot = take_snapshot(connection)
    assert not snapshot.exact
    assert snapshot.schema[("table", "v", "v")] == "v"
    assert ("columns", "t", "t") in snapshot.schema


class FakeCursor:
    """Answers information_schema queries with canned rows."""

    ROWS: dict[str, list[Any]] = {
        "information_schema.columns": [
            ("t", "b", 2, "int", "YES", None, "", None, None),
            ("t", "a", 1, "int", "NO", None, "auto_increment", None, None),
        ],
        "information_schema.statistics": [("t", "PRIMARY", 0, 1, "a", None, "BTREE")],
        "information_schema.table_constraints": [("t", "PRIMARY", "PRIMARY KEY")],
        "information_schema.key_column_usage": [("t", "PRIMARY", "a", 1, None, None)],
        "information_schema.check_constraints": [("t_chk", "(b > 0)")],
        "information_schema.triggers": [("t", "tr", "AFTER", "INSERT", "SET @x = 1")],
        "information_schema.views": [("v", "select 1")],
        "information_schema.routines": [("f", "FUNCTION", "RETURN 1")],
    }

    def __init__(self) -> None:
        self.rows: list[Any] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.rows = next(
            (rows for table, rows in self.ROWS.items() if table in sql), []
        )

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


def test_mysql_schema() -> None:
    schema: dict[Any, Any] = {}
    snapshot_module._mysql_schema(FakeCursor(), schema)
    assert schema[("columns", "t", "t")][0][0] == "a"
    assert schema[("index", "t", "PRIMARY")] == [(1, "a", None, 0, "BTREE")]
    assert schema[("trigger", "t", "tr")] == ("AFTER", "INSERT", "SET @x = 1")
    assert {key[0] for key in schema} == {
        "columns",
        "index",
        "constraint",
        "key column",
        "check",
        "trigger",
        "view",
        "routine",
    }


def test_mysql_vendor_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        vendor = "mysql"

        def cursor(self) -> Any:
            class Context:
                def __enter__(self) -> FakeCursor:
                    return FakeCursor()

                def __exit__(self, *args: Any) -> None:
                    pass

            return Context()

    monkeypatch.setattr(snapshot_module, "_data", lambda *args: None)
    snapshot = take_snapshot(Connection())  # type: ignore[arg-type]
    assert snapshot.exact
    assert ("view", None, "v") in snapshot.schema


# Scratch databases without a server


def test_mirror_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.db.utils import ConnectionHandler

    sqlite = "django.db.backends.sqlite3"
    handler = ConnectionHandler(
        {
            "default": {"ENGINE": sqlite, "NAME": "a"},
            "broken": {"ENGINE": sqlite, "NAME": "b", "TEST": {"MIRROR": "nowhere"}},
        }
    )
    monkeypatch.setattr(scratch_module, "connections", handler)
    with pytest.raises(ScratchDatabaseError, match="mirrors 'nowhere'"):
        scratch_module._groups("default")
    handler = ConnectionHandler(
        {
            "default": {"ENGINE": sqlite, "NAME": "a"},
            "m1": {"ENGINE": sqlite, "NAME": "b", "TEST": {"MIRROR": "m2"}},
            "m2": {"ENGINE": sqlite, "NAME": "c", "TEST": {"MIRROR": "default"}},
        }
    )
    monkeypatch.setattr(scratch_module, "connections", handler)
    (group,) = scratch_module._groups("default")
    assert sorted(group.aliases) == ["default", "m1", "m2"]


def test_dummy_alias_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from django.db.utils import ConnectionHandler

    handler = ConnectionHandler({"default": {"ENGINE": "django.db.backends.dummy"}})
    monkeypatch.setattr(scratch_module, "connections", handler)
    with pytest.raises(ScratchDatabaseError, match="dummy backend"):
        scratch_database("default").__enter__()


def test_signals_outside_main_thread(
    django_db_blocker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    restores: list[Any] = []
    thread = threading.Thread(
        target=lambda: restores.append(scratch_module._exit_on_termination())
    )
    thread.start()
    thread.join()
    restores[0]()
    import signal

    monkeypatch.delattr(signal, "SIGHUP")
    scratch_module._exit_on_termination()()


def test_drop_failure_reported() -> None:
    class Creation:
        def sql_table_creation_suffix(self) -> str:
            return ""

        def _nodb_cursor(self) -> Any:
            raise OSError("no server")

    class Connection:
        vendor = "mysql"
        creation = Creation()

        class ops:  # noqa: N801
            @staticmethod
            def quote_name(name: str) -> str:
                return name

    with pytest.raises(ScratchDatabaseError, match="please drop it by hand"):
        scratch_module._drop(Connection(), "cleanmig_x")
    with pytest.raises(ScratchDatabaseError, match="Could not create"):
        scratch_module._create(Connection(), "cleanmig_x")


def test_leftovers_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connections["default"], "vendor", "oracle")
    with pytest.raises(ScratchDatabaseError, match="aren't supported"):
        list_leftovers()


# PostgreSQL


def _postgres() -> dict[str, Any]:
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "cm_dev_tests",
        "HOST": os.environ.get("CLEANMIG_TEST_POSTGRES_HOST", "127.0.0.1"),
        "PORT": os.environ.get("CLEANMIG_TEST_POSTGRES_PORT", "5432"),
        "USER": os.environ.get("CLEANMIG_TEST_POSTGRES_USER", "rw"),
        "PASSWORD": os.environ.get("CLEANMIG_TEST_POSTGRES_PASSWORD", "rw"),
    }


@pytest.fixture
def pg(django_db_blocker: Any) -> Iterator[str]:
    """An alias "pg" for the PostgreSQL server, or skip."""
    import psycopg

    config = _postgres()
    try:
        psycopg.connect(
            host=config["HOST"],
            port=config["PORT"],
            user=config["USER"],
            password=config["PASSWORD"],
            dbname="postgres",
            connect_timeout=3,
        ).close()
    except psycopg.Error:  # pragma: no cover - CI has a server
        pytest.skip("no PostgreSQL server")
    connections.settings["pg"] = connections.configure_settings(
        {"default": {"ENGINE": "django.db.backends.sqlite3"}, "pg": config}
    )["pg"]
    settings.DATABASES["pg"] = connections.settings["pg"]
    try:
        with django_db_blocker.unblock():
            yield "pg"
    finally:
        connections["pg"].close()
        del connections["pg"]
        connections.settings.pop("pg", None)
        settings.DATABASES.pop("pg", None)


def test_postgres_snapshot(pg: str) -> None:
    with scratch_database(pg) as connection:
        assert connection.settings_dict["NAME"].startswith("cleanmig_")
        with connection.cursor() as cursor:
            for sql in (
                "CREATE TYPE mood AS ENUM ('a', 'b')",
                "CREATE TABLE t (id serial PRIMARY KEY, m mood,"
                " n integer CHECK (n > 0), b bytea)",
                "CREATE INDEX t_n ON t (n)",
                "COMMENT ON COLUMN t.n IS 'number'",
                "CREATE VIEW v AS SELECT id FROM t",
                "CREATE MATERIALIZED VIEW mv AS SELECT id FROM t",
                "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS"
                " $$ BEGIN RETURN NEW; END $$",
                "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW EXECUTE FUNCTION f()",
                "INSERT INTO t (m, n, b) VALUES ('a', 1, '\\x00ff')",
            ):
                cursor.execute(sql)
        snapshot = take_snapshot(connection)
    kinds = {key[0] for key in snapshot.schema}
    assert {
        "columns",
        "constraint",
        "index",
        "sequence",
        "sequence owner",
        "view",
        "materialized view",
        "function",
        "trigger",
        "enum",
        "extension",
        "comment",
    } <= kinds
    assert snapshot.columns["t"] == ("b", "id", "m", "n")
    assert ("b'\\x00\\xff'", "1", "'a'", "1") in snapshot.data["t"]
    assert sum(snapshot.data["t"].values()) == 1
    assert list_leftovers(pg) == [] or all(
        name.startswith("cleanmig_") for name in list_leftovers(pg)
    )


def test_postgres_drop_forced(pg: str) -> None:
    import psycopg

    config = _postgres()
    with scratch_database(pg) as connection:
        name = connection.settings_dict["NAME"]
        # A connection the command doesn't know about: plain DROP DATABASE
        # fails, WITH (FORCE) ends it.
        other = psycopg.connect(
            host=config["HOST"],
            port=config["PORT"],
            user=config["USER"],
            password=config["PASSWORD"],
            dbname=name,
        )
    with pytest.raises(psycopg.OperationalError):
        other.execute("SELECT 1")
    other.close()
    assert name not in list_leftovers(pg)


def test_postgres_leftovers(pg: str) -> None:
    connection = connections[pg]
    name = scratch_module._scratch_name("leftover", 63)
    scratch_module._create(connection, name)
    try:
        assert name in list_leftovers(pg)
    finally:
        drop_leftover(name, pg)
    assert name not in list_leftovers(pg)
    with pytest.raises(ScratchDatabaseError, match="not a scratch database"):
        drop_leftover("postgres", pg)


def test_postgres_drop_failure_reported(
    pg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = scratch_module._drop

    def drop(connection: Any, name: str) -> None:
        real(connection, name)
        raise ScratchDatabaseError(f"Could not drop the scratch database {name!r}")

    monkeypatch.setattr(scratch_module, "_drop", drop)
    with pytest.raises(ScratchDatabaseError, match="Could not drop"):  # noqa: SIM117
        with scratch_database(pg):
            assert connections[pg].vendor == "postgresql"


def test_postgres_trial(pg: str, project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "legacy", models.IntegerField(default=0)),\n'
        "migrations.RunPython(forwards),",
        extra="def forwards(apps, schema_editor):\n"
        "    alias = schema_editor.connection.alias\n"
        "    apps.get_model('shop', 'Item').objects.using(alias).filter(name='legacy')"
        ".count()",
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )
    result = project.check_equivalence(trial=True, database=pg, noop_data=True)
    assert result.verification["vendor"] == "postgresql"
    assert sorted(r.kind for r in result.removals) == ["field", "noop"]


def test_memoryview_values() -> None:
    # psycopg2 returns bytea as memoryview.
    assert _value(memoryview(b"ab")) == "b'ab'"


def test_postgres_scratch_name_taken(pg: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Scratch names are random: never a configured database's, but checked.
    monkeypatch.setattr(scratch_module, "_scratch_name", lambda name, length: name)
    with pytest.raises(ScratchDatabaseError, match="it is a real database's name"):
        scratch_database(pg).__enter__()
