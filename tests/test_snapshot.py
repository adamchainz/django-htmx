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
        kinds = {key[0] for key in snapshot.schema}
        # The next id, which survives the deleted row, isn't compared.
        assert "next id" not in kinds
        assert {"check", "view", "trigger"} <= kinds
        assert ("check", "t", "b_positive:['b']") in snapshot.schema
        # Dates and bytes are compared as values.
        assert snapshot.columns["t"] == ("a", "b", "c", "d", "e")
        assert list(snapshot.data["t"]) == [
            ("1", "1", "'x'", "datetime.datetime(2026, 1, 1, 0, 0)", "b'\\x00\\xff'")
        ]
        masked = Mask(tables={"t"})
        assert snap(data={"t": Counter({("1",): 1})}).differences(snap(), masked) == []

    def test_content_type_and_permission_ids_not_compared(self):
        def snapshot(first: int, second: int, permissions: bool = True) -> Snapshot:
            with self.db_blocker.unblock(), scratch_database() as connection:
                with connection.cursor() as cursor:
                    statements = [
                        "CREATE TABLE django_content_type (id integer PRIMARY KEY"
                        " AUTOINCREMENT, app_label text, model text)",
                        "CREATE TABLE owner (id integer PRIMARY KEY)",
                        "CREATE TABLE note (id integer PRIMARY KEY,"
                        " content_type_id integer REFERENCES django_content_type (id),"
                        " owner_id integer REFERENCES owner (id),"
                        " permission_id integer, plain integer)",
                        "INSERT INTO owner (id) VALUES (1)",
                        f"INSERT INTO django_content_type (id, app_label, model)"
                        f" VALUES ({first}, 'shop', 'item'), ({second}, 'shop', 'tag')",
                        f"INSERT INTO note VALUES (1, {first}, 1, NULL, {first})",
                    ]
                    if permissions:
                        statements += [
                            "CREATE TABLE auth_permission (id integer PRIMARY KEY"
                            " AUTOINCREMENT, content_type_id integer REFERENCES"
                            " django_content_type (id), codename text)",
                            "CREATE TABLE grants (permission_id integer"
                            " REFERENCES auth_permission (id))",
                            f"INSERT INTO auth_permission VALUES"
                            f" ({second}, {first}, 'add_item')",
                            f"INSERT INTO grants VALUES ({second})",
                        ]
                    for sql in statements:
                        cursor.execute(sql)
                return take_snapshot(connection)

        a, b = snapshot(1, 2), snapshot(2, 1)
        assert not any(key[0] == "next id" for key in a.schema)
        assert a.data["django_content_type"] == Counter(
            {
                ("'shop'", "'<content type shop.item>'", "'item'"): 1,
                ("'shop'", "'<content type shop.tag>'", "'tag'"): 1,
            }
        )
        assert a.data["grants"] == Counter(
            {("'<permission <content type shop.item> add_item>'",): 1}
        )
        # Only ids and foreign keys are translated: the plain column differs.
        plain = Mask(columns={"note": {"plain"}})
        assert [str(d) for d in a.differences(b)] == [
            "data of table note differs (1 row(s) missing, 1 unexpected)"
        ]
        assert a.differences(b, plain) == []
        c, d = snapshot(1, 2, permissions=False), snapshot(2, 1, permissions=False)
        assert c.differences(d, plain) == []

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
        # The sequence, without its last value (the next id).
        assert snapshot.schema[("sequence", None, "public.t_id_seq")] == (
            "integer",
            1,
            1,
            2147483647,
            1,
            False,
        )
        # Columns are compared by name, not in their order.
        assert [c[0] for c in snapshot.schema[("columns", "t", "public.t")]] == [
            "b",
            "id",
            "m",
            "n",
        ]
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
        # Columns are compared by name, not in their order.
        assert [c[0] for c in base.schema[("columns", "t", "t")]] == [
            "body",
            "id",
            "n",
            "name",
            "p",
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
            ]
        ],
    )
    def test_mysql_differences(self, server, table, what):
        my = self.server(server)
        base = my_snapshot(my, MY_TABLE)
        other = my_snapshot(my, table)
        details = [d.detail for d in base.differences(other, Mask())]
        assert any(d.startswith(what) for d in details), details

    @parametrize("server", [("mysql",), ("mariadb",)])
    def test_mysql_order_and_next_id_not_compared(self, server):
        my = self.server(server)
        base = my_snapshot(my, MY_TABLE)
        other = my_snapshot(
            my,
            MY_TABLE.replace(
                " p int NOT NULL, n int CHECK (n > 0),",
                " n int CHECK (n > 0), p int NOT NULL,",
            ),
            "INSERT INTO t (p) VALUES (1)",
            "DELETE FROM t",
        )
        assert base.differences(other) == []

    def test_memoryview_values(self):
        assert _value(memoryview(b"ab")) == "b'ab'"
