"""Snapshots on every backend, and scratch databases on PostgreSQL.

The PostgreSQL tests use the server from compose.yaml, so start it with:
docker compose up -d --wait
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from unittest_parametrize import parametrize

from django_compact_migrations.scratch import (
    list_leftovers,
    scratch_database,
)
from django_compact_migrations.snapshot import Mask, Snapshot, _value, take_snapshot
from tests.project import ProjectTestCase


def snap(**kwargs: Any) -> Snapshot:
    return Snapshot("sqlite", **kwargs)


MY_TABLE = (
    "CREATE TABLE t (id int AUTO_INCREMENT PRIMARY KEY,"
    " p int NOT NULL, n int CHECK (n > 0), body text DEFAULT ('x'),"
    " name varchar(10) COLLATE utf8mb4_bin COMMENT 'the name',"
    " KEY t_p (p)) COMMENT 'things'"
)


def my_snapshot(alias: str, *statements: str) -> Snapshot:
    with scratch_database(alias) as connection:
        with connection.cursor() as cursor:
            for sql in statements:
                cursor.execute(sql)
        return take_snapshot(connection)


class SnapshotTests(ProjectTestCase):
    def test_column_differences_named(self):
        a = snap(schema={("columns", "t", "t"): [("a", "int"), ("b", "int")]})
        b = snap(schema={("columns", "t", "t"): [("a", "int"), ("b", "text")]})
        (diff,) = a.differences(b)
        assert diff.columns == ("b",)
        assert str(diff) == "schema of table t differs (columns t changed: b)"
        moved = snap(schema={("columns", "t", "t"): [("b", "int"), ("a", "int")]})
        assert a.differences(moved)[0].columns == ("a", "b")
        assert a.differences(b, Mask(schema={("columns", "t", "t")})) == []

    def test_without_migrations(self):
        a = snap()
        assert a.without_migrations([("shop", "0001")]) is a
        b = snap(data={"django_migrations": Counter({("'shop'", "'0001'"): 1})})
        assert b.without_migrations([("shop", "0001")]).data["django_migrations"] == {}

    def test_nondeterminism(self):
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

    def test_sqlite_objects(self):
        with self.db_blocker.unblock(), scratch_database() as connection:
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
                cursor.execute(
                    "CREATE TRIGGER tr AFTER INSERT ON t BEGIN SELECT 1; END"
                )
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

    def test_postgres_snapshot(self):
        pg = self.server("pg")
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

    @parametrize("server", [("mysql",), ("mariadb",)])
    def test_mysql_snapshot(self, server):
        my = self.server(server)
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

    @parametrize(
        ("server", "table", "what"),
        [
            (server, *row)
            for server in ("mysql", "mariadb")
            for row in [
                (
                    MY_TABLE.replace("KEY t_p (p)", "KEY t_p2 (p)"),
                    "index t_p2 unexpected",
                ),
                (
                    MY_TABLE.replace("DEFAULT ('x')", "DEFAULT ('y')"),
                    "columns t changed: body",
                ),
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
            ]
        ],
    )
    def test_mysql_differences(self, server, table, what):
        my = self.server(server)
        base = my_snapshot(my, MY_TABLE)
        other = my_snapshot(my, table)
        details = [d.detail for d in base.differences(other, Mask())]
        assert any(d.startswith(what) for d in details), details

    def test_memoryview_values(self):
        assert _value(memoryview(b"ab")) == "b'ab'"
