"""Snapshots on every backend, and scratch databases on PostgreSQL.

The PostgreSQL tests use the server from compose.yaml, so start it with:
docker compose up -d --wait
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any

import pytest
from django.db import connections

from django_compact_migrations import backends
from django_compact_migrations import scratch as scratch_module
from django_compact_migrations.scratch import (
    ScratchDatabaseError,
    drop_leftover,
    list_leftovers,
    scratch_database,
)
from django_compact_migrations.snapshot import Mask, Snapshot, _value, take_snapshot
from tests.project import ITEM, SERVERS, blocked

# Comparing snapshots


def snap(**kwargs: Any) -> Snapshot:
    return Snapshot("sqlite", **kwargs)


def test_column_differences_named():
    a = snap(schema={("columns", "t", "t"): [("a", "int"), ("b", "int")]})
    b = snap(schema={("columns", "t", "t"): [("a", "int"), ("b", "text")]})
    (diff,) = a.differences(b)
    assert diff.columns == ("b",)
    assert str(diff) == "schema of table t differs (columns t changed: b)"
    moved = snap(schema={("columns", "t", "t"): [("b", "int"), ("a", "int")]})
    assert a.differences(moved)[0].columns == ("a", "b")
    assert a.differences(b, Mask(schema={("columns", "t", "t")})) == []


def test_without_migrations():
    a = snap()
    assert a.without_migrations([("shop", "0001")]) is a
    b = snap(data={"django_migrations": Counter({("'shop'", "'0001'"): 1})})
    assert b.without_migrations([("shop", "0001")]).data["django_migrations"] == {}


def test_nondeterminism():
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


def test_sqlite_objects(django_db_blocker):
    with django_db_blocker.unblock(), scratch_database() as connection:
        # No AUTOINCREMENT table yet: no sqlite_sequence.
        assert take_snapshot(connection).schema == {}
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
            cursor.execute(
                "CREATE TABLE s (id integer PRIMARY KEY AUTOINCREMENT, n int)"
            )
            cursor.execute("INSERT INTO s (n) VALUES (1)")
            cursor.execute("DELETE FROM s")
        snapshot = take_snapshot(connection)
    # The next id survives the deleted row.
    assert snapshot.schema[("next id", "s", "s")] == 1
    kinds = {key[0] for key in snapshot.schema}
    assert {"check", "view", "trigger"} <= kinds
    assert ("check", "t", "b_positive:['b']") in snapshot.schema
    # Dates and bytes are compared as values.
    assert snapshot.columns["t"] == ("a", "b", "c", "d", "e")
    assert list(snapshot.data["t"]) == [
        ("1", "1", "'x'", "datetime.datetime(2026, 1, 1, 0, 0)", "b'\\x00\\xff'")
    ]
    masked = Mask(tables={"t"})
    assert snap(data={"t": Counter({("1",): 1})}).differences(snap(), masked) == []


# Scratch databases without a server


def test_mirror_errors(monkeypatch):
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


def test_dummy_alias_refused(monkeypatch):
    from django.db.utils import ConnectionHandler

    handler = ConnectionHandler({"default": {"ENGINE": "django.db.backends.dummy"}})
    monkeypatch.setattr(scratch_module, "connections", handler)
    with pytest.raises(ScratchDatabaseError, match="dummy backend"):
        scratch_database("default").__enter__()


def test_signals_outside_main_thread(django_db_blocker, monkeypatch):
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


def test_drop_failure_reported():
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
        scratch_module._drop(Connection(), "compactmig_x")
    with pytest.raises(ScratchDatabaseError, match="Could not create"):
        scratch_module._create(Connection(), "compactmig_x")


def test_leftovers_unsupported(monkeypatch):
    monkeypatch.setattr(connections["default"], "vendor", "oracle")
    with pytest.raises(ScratchDatabaseError, match="aren't supported"):
        list_leftovers()


# PostgreSQL


def test_postgres_snapshot(pg):
    with scratch_database(pg) as connection:
        assert connection.settings_dict["NAME"].startswith("compactmig_")
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
    # The sequence's last value: the next id.
    assert snapshot.schema[("sequence", None, "public.t_id_seq")][-1] == 1
    assert sum(snapshot.data["t"].values()) == 1
    assert list_leftovers(pg) == [] or all(
        name.startswith("compactmig_") for name in list_leftovers(pg)
    )


def test_postgres_drop_forced(pg):
    import psycopg

    config = SERVERS[pg]
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


def test_postgres_leftovers(pg):
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


def test_postgres_drop_failure_reported(pg, monkeypatch):
    real = scratch_module._drop

    def drop(connection: Any, name: str) -> None:
        real(connection, name)
        raise ScratchDatabaseError(f"Could not drop the scratch database {name!r}")

    monkeypatch.setattr(scratch_module, "_drop", drop)
    with pytest.raises(ScratchDatabaseError, match="Could not drop"):  # noqa: SIM117
        with scratch_database(pg):
            assert connections[pg].vendor == "postgresql"


def test_postgres_trial(pg, project):
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
    result = project.check_equivalence(static=False, database=pg, noop_data=True)
    assert result.verification["vendor"] == "postgresql"
    assert sorted(r.kind for r in result.removals) == ["field", "noop"]


def test_memoryview_values():
    # psycopg2 returns bytea as memoryview.
    assert _value(memoryview(b"ab")) == "b'ab'"


def test_postgres_scratch_name_taken(pg, monkeypatch):
    # Scratch names are random: never a configured database's, but checked.
    monkeypatch.setattr(scratch_module, "_scratch_name", lambda name, length: name)
    with pytest.raises(ScratchDatabaseError, match="it is a real database's name"):
        scratch_database(pg).__enter__()


# MySQL and MariaDB

MY_TABLE = (
    "CREATE TABLE t (id int AUTO_INCREMENT PRIMARY KEY,"
    " p int NOT NULL, n int CHECK (n > 0), body text DEFAULT ('x'),"
    " name varchar(10) COLLATE utf8mb4_bin COMMENT 'the name',"
    " KEY t_p (p)) COMMENT 'things'"
)


SERVERS_KIND = {"mysql": backends.MYSQL, "mariadb": backends.MARIADB}
SERVER_NAMES = {"mysql": "MySQL", "mariadb": "MariaDB"}


def my_snapshot(alias: str, *statements: str) -> Snapshot:
    with scratch_database(alias) as connection:
        with connection.cursor() as cursor:
            for sql in statements:
                cursor.execute(sql)
        return take_snapshot(connection)


def test_mysql_snapshot(my):
    base = my_snapshot(
        my,
        "CREATE TABLE parent (id int PRIMARY KEY)",
        MY_TABLE,
        "ALTER TABLE t ADD CONSTRAINT t_fk FOREIGN KEY (p) REFERENCES parent (id)",
        "CREATE VIEW v AS SELECT id FROM t",
        "CREATE TRIGGER tr BEFORE INSERT ON t FOR EACH ROW SET NEW.n = 1",
        "CREATE FUNCTION f() RETURNS int DETERMINISTIC RETURN 1",
        "INSERT INTO parent VALUES (1)",
        "INSERT INTO t (p, n, name) VALUES (1, 2, 'a')",
    )
    assert {key[0] for key in base.schema} >= {
        "table",
        "columns",
        "index",
        "constraint",
        "key column",
        "foreign key",
        "check",
        "trigger",
        "view",
        "routine",
    }
    # The next id; parent has no AUTO_INCREMENT.
    assert base.schema[("next id", "t", "t")] == "2"
    assert ("next id", "parent", "parent") not in base.schema
    assert [c[0] for c in base.schema[("columns", "t", "t")]] == [
        "id",
        "p",
        "n",
        "body",
        "name",
    ]
    # body, id, n (the trigger's), name, p
    assert base.data["t"] == Counter({("'x'", "1", "1", "'a'", "1"): 1})
    # Checks are told apart by table (MariaDB names them after the column).
    assert [k for k in base.schema if k[0] == "check"] == [("check", "t", "n")] or [
        k for k in base.schema if k[0] == "check"
    ] == [("check", "t", "t_chk_1")]


@pytest.mark.parametrize(
    ("table", "what"),
    [
        (MY_TABLE.replace("KEY t_p (p)", "KEY t_p2 (p)"), "index t_p2 unexpected"),
        (MY_TABLE.replace("DEFAULT ('x')", "DEFAULT ('y')"), "columns t changed: body"),
        (
            MY_TABLE.replace("utf8mb4_bin", "utf8mb4_general_ci"),
            "columns t changed: name",
        ),
        (MY_TABLE.replace("'the name'", "'a name'"), "columns t changed: name"),
        (MY_TABLE.replace("'things'", "'stuff'"), "table t changed"),
        (
            MY_TABLE.replace(
                " p int NOT NULL, n int CHECK (n > 0),",
                " n int CHECK (n > 0), p int NOT NULL,",
            ),
            "columns t changed",
        ),
    ],
)
def test_mysql_differences(my, table, what):
    base = my_snapshot(my, MY_TABLE)
    other = my_snapshot(my, table)
    details = [d.detail for d in base.differences(other, Mask())]
    assert any(d.startswith(what) for d in details), details


def test_mysql_trial(my, project):
    # The removal the static rules keep on MySQL (see test_backends), found
    # by the trial when the rules don't know the server.
    project.migration(
        "shop",
        "0001_initial",
        """
        migrations.CreateModel("Parent", [("id", models.AutoField(primary_key=True))]),
        migrations.CreateModel(
            "Child",
            [
                ("id", models.AutoField(primary_key=True)),
                ("x", models.IntegerField()),
                ("parent", models.ForeignKey("shop.parent", models.CASCADE)),
            ],
        ),
        """,
    )
    project.migration(
        "shop",
        "0002_add",
        'migrations.AlterUniqueTogether("child", {("parent", "x")}),',
    )
    project.migration(
        "shop", "0003_drop", 'migrations.AlterUniqueTogether("child", set()),'
    )
    static = project.compact()
    assert static.options["backends"] == [SERVERS_KIND[my]]
    # Only adding the entry folds; dropping it again is kept (see backends).
    assert [r.description for r in static.removals] == [
        "AlterUniqueTogether in 0002_add folded into CreateModel in 0001_initial"
    ]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(backends, "configured", lambda: frozenset())
        assert project.compact().removals
        result = project.check_equivalence(static=False, database=my)
    assert result.removals == []
    (reason,) = blocked(result)["shop.Child unique_together (parent, x)"]
    assert reason.startswith("trial: schema of table shop_child differs (index")
    info = result.verification
    assert (info["vendor"], info["server"]) == ("mysql", SERVER_NAMES[my])


def test_mysql_noop_trial(my, project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_data",
        "migrations.RunPython(migrations.RunPython.noop),",
    )
    project.migration(
        "shop",
        "0003_qty",
        'migrations.AddField("item", "qty", models.TextField(default="a")),',
    )
    result = project.check_equivalence(
        static=False, noop_data=True, prune=True, database=my
    )
    assert [r.kind for r in result.removals if r.kind == "noop"] == ["noop"]
    # The TEXT default: kept out of the CreateModel on MySQL only.
    folded = any(r.kind == "fold" for r in result.removals)
    assert folded is (my == "mariadb")
