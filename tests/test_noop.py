from __future__ import annotations

import textwrap
import tomllib
from typing import Any

import pytest
from django.core.management import CommandError
from unittest_parametrize import parametrize

from django_compact_migrations.noop import (
    Observation,
    classify_statement,
    observe,
    source_concern,
    written_tables,
)
from django_compact_migrations.scratch import scratch_database
from tests import noop_samples as samples
from tests.project import (
    ITEM,
    Project,
    ProjectTestCase,
    kinds,
    temp_database,
)

NOOP: dict[str, Any] = {"noop_data": True, "skip_kinds": ["fold"]}


def data_migration(project: Project, code: str, *, extra_ops: str = "") -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
    )
    project.migration(
        "shop",
        "0003_data",
        extra_ops + "migrations.RunPython(forwards),",
        extra="def forwards(apps, schema_editor):\n"
        "    Item = apps.get_model('shop', 'Item')\n"
        "    alias = schema_editor.connection.alias\n"
        + "\n".join(
            f"    {line}" for line in textwrap.dedent(code).strip().splitlines()
        ),
    )
    project.migration(
        "shop", "0004_remove", 'migrations.RemoveField("item", "legacy"),'
    )


def noops(result: Any) -> list[str]:
    return [r.description for r in result.removals if r.kind == "noop"]


class NoopTests(ProjectTestCase):
    def test_loop_over_empty_table(self):
        data_migration(
            self.project,
            """
            for item in Item.objects.using(alias).filter(legacy=1):
                item.save()
            """,
        )
        result = self.project.check_equivalence(**NOOP)
        assert len(noops(result)) == 1
        # Without the loop, nothing mentions "legacy": its field goes too.
        assert ("field", "shop.Item.legacy") in kinds(result)
        (noop,) = result.verification["noop"]
        assert noop["migration"] == ["shop", "0003_data"]
        assert noop["note"] == "no writes on a fresh SQLite database: 1 query"
        assert result.removals[0].kind in ("noop", "field")

    def test_seed_row_kept(self):
        data_migration(
            self.project, 'Item.objects.using(alias).create(name="x", legacy=1)'
        )
        result = self.project.check_equivalence(**NOOP)
        assert noops(result) == []

    def test_update_zero_rows_removed(self):
        data_migration(self.project, "Item.objects.using(alias).update(legacy=2)")
        assert len(noops(self.project.check_equivalence(**NOOP))) == 1

    def test_update_seeded_row_kept(self):
        data_migration(
            self.project,
            "Item.objects.using(alias).update(legacy=2)",
            extra_ops="migrations.RunSQL(\"INSERT INTO shop_item (name, legacy) VALUES ('a', 0)\"),\n",
        )
        assert noops(self.project.check_equivalence(**NOOP)) == []

    def test_run_sql(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM
            + """
            migrations.RunSQL("UPDATE shop_item SET name = 'x'"),
            migrations.RunSQL("CREATE INDEX extra_idx ON shop_item (name)"),
            """,
        )
        result = self.project.check_equivalence(**NOOP)
        assert len(noops(result)) == 1
        assert "UPDATE" not in self.project.read("shop", "0001_initial")
        assert "CREATE INDEX" in self.project.read("shop", "0001_initial")

    def test_other_alias_kept(self):
        data_migration(
            self.project,
            """
            from django.db import connections
            for name in connections:
                if name != alias:
                    with connections[name].cursor() as cursor:
                        cursor.execute("SELECT 1")
            """,
        )
        with temp_database(self.tmp_path):
            result = self.project.check_equivalence(**NOOP)
        assert noops(result) == []

    def test_excluded_app_kept(self):
        data_migration(self.project, "Item.objects.using(alias).update(legacy=2)")
        assert noops(self.project.check_equivalence(exclude=["shop"], **NOOP)) == []

    def test_fold_unlocked(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_data",
            "migrations.RunPython(forwards),",
            extra="""
            def forwards(apps, schema_editor):
                alias = schema_editor.connection.alias
                apps.get_model("shop", "Item").objects.using(alias).update(name="x")
            """,
        )
        self.project.migration(
            "shop",
            "0003_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
        )
        result = self.project.check_equivalence(noop_data=True)
        assert sorted(r.kind for r in result.removals) == ["fold", "noop"]

    def test_plan_file(self):
        data_migration(self.project, "Item.objects.using(alias).update(legacy=2)")
        plan_path = self.tmp_path / "plan.toml"
        out, _ = self.project.command("plan", "--noop-data", "-o", str(plan_path))
        assert "noop" in out
        data = tomllib.loads(plan_path.read_text())
        assert data["verification"]["noop"]
        assert data["options"]["noop_data"] is True
        self.project.command("apply", "--plan", str(plan_path))
        assert (
            "RunPython"
            not in self.project.read("shop", "0003_data").split("operations")[-1]
        )

    def test_needs_trial_backend(self):
        data_migration(self.project, "pass")
        with pytest.raises(CommandError, match="can't be combined with --static"):
            self.project.command("plan", "--noop-data", "--static")

    @parametrize(
        ("sql", "rowcount", "write"),
        [
            ("SELECT 1", None, False),
            ('SELECT "a" FROM "t" WHERE "b" IN (1, 2)', None, False),
            ("SELECT COUNT(*) FROM t", None, False),
            ("-- comment\nSELECT 1", None, False),
            ("SAVEPOINT s1", None, False),
            ("UPDATE t SET a = 1", 0, False),
            ("UPDATE t SET a = 1", 3, True),
            ("UPDATE t SET a = 1", -1, True),
            ("DELETE FROM t", None, True),
            ("INSERT INTO t VALUES (1)", 1, True),
            ("WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x", 1, True),
            ("SELECT nextval('s')", None, True),
            ("SELECT a INTO b FROM t", None, True),
            ("SELECT my_func()", None, True),
            ("CREATE TABLE x (a int)", None, True),
            ("SET search_path TO x", None, True),
            ("PRAGMA foreign_keys = 0", None, True),
        ],
    )
    def test_classify_statement(self, sql, rowcount, write):
        assert (classify_statement(sql, rowcount) is not None) is write

    @parametrize(
        ("func", "statements", "concern"),
        [
            (samples._registers, 0, "signals"),
            (samples._vendor, 1, ".vendor"),
            (samples._sequences, 1, "sequence_reset"),
            (samples._files, 1, "open"),
            (samples._quiet, 0, None),
            (samples._local_cache, 1, None),
            (samples._calls_something, 0, "helper_called"),
            (samples._loop, 1, None),
            (samples._loop, 0, "calls all()"),
            (print, 0, "can't be read"),
        ],
    )
    def test_source_concern(self, func, statements, concern):
        found = source_concern(func, statements)
        if concern is None:
            assert found is None
        else:
            assert found is not None and concern in found

    def test_signal_registration_kept(self):
        data_migration(
            self.project,
            """
            from django.db.models.signals import post_migrate
            post_migrate.connect(lambda **kwargs: None, weak=False)
            """,
        )
        result = self.project.check_equivalence(**NOOP)
        assert noops(result) == []
        (reason,) = result.verification["noop_kept"].values()
        assert "signals" in reason

    def test_vendor_branch_kept(self):
        data_migration(
            self.project,
            """
            if schema_editor.connection.vendor == "postgresql":
                schema_editor.execute("SELECT setval('x', 1)")
            """,
        )
        assert noops(self.project.check_equivalence(**NOOP)) == []

    def test_plain_progress_without_terminal(self):
        data_migration(self.project, "Item.objects.using(alias).update(legacy=2)")
        _, err = self.project.command("plan", "--noop-data")
        assert "Baseline: migration 1/" in err
        assert "Trial run 1: migration 1/" in err

    @parametrize(
        ("sql", "tables"),
        [
            (
                'INSERT INTO "django_content_type" ("app_label") VALUES (%s)',
                {"django_content_type"},
            ),
            ('UPDATE "shop_item" SET "name" = 1', {"shop_item"}),
            ("DELETE FROM public.shop_item WHERE id = 1", {"shop_item"}),
            ("SELECT setval('x', 1)", {"*"}),
        ],
    )
    def test_written_tables(self, sql, tables):
        assert written_tables(sql) == tables

    @parametrize(
        ("sql", "rowcount", "write"),
        [
            ("-- only a comment", None, False),
            ("SELECT 1; SELECT 2", None, False),
            ("SELECT 1; DELETE FROM t", None, True),
            ("SET CONSTRAINTS ALL IMMEDIATE", None, False),
        ],
    )
    def test_classify_more(self, sql, rowcount, write):
        assert (classify_statement(sql, rowcount) is not None) is write

    def test_executemany_update_counts_as_write(self):
        observation = Observation()
        with self.db_blocker.unblock(), scratch_database() as connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE TABLE t (a int)")
            with observe(connection.alias, observation), connection.cursor() as cursor:
                cursor.executemany("UPDATE t SET a = %s", [(1,), (2,)])
        assert observation.writes == ["UPDATE affected unknown rows"]
        assert observation.tables == {"t"}

    def test_source_concern_more(self):
        assert (
            source_concern(samples._calls_subscript, 0)
            == "it ran no query, yet calls something()"
        )
        assert source_concern(samples._imports_json, 1) is None
        assert (
            source_concern(samples._calls_unreadable, 1) == "its source can't be read"
        )
