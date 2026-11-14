from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError

from django_clean_migrations.noop import classify
from tests.project import Project, temp_database
from tests.test_clean import ITEM, kinds

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


def test_loop_over_empty_table(project: Project) -> None:
    data_migration(
        project,
        """
        for item in Item.objects.using(alias).filter(legacy=1):
            item.save()
        """,
    )
    result = project.check_equivalence(**NOOP)
    assert len(noops(result)) == 1
    # Without the loop, nothing mentions "legacy": its field goes too.
    assert ("field", "shop.Item.legacy") in kinds(result)
    (noop,) = result.verification["noop"]
    assert noop["migration"] == ["shop", "0003_data"]
    assert noop["note"] == "no writes on a fresh database: 1 query"
    assert result.removals[0].kind in ("noop", "field")


def test_seed_row_kept(project: Project) -> None:
    data_migration(project, 'Item.objects.using(alias).create(name="x", legacy=1)')
    result = project.check_equivalence(**NOOP)
    assert noops(result) == []


def test_update_zero_rows_removed(project: Project) -> None:
    data_migration(project, "Item.objects.using(alias).update(legacy=2)")
    assert len(noops(project.check_equivalence(**NOOP))) == 1


def test_update_seeded_row_kept(project: Project) -> None:
    data_migration(
        project,
        "Item.objects.using(alias).update(legacy=2)",
        extra_ops="migrations.RunSQL(\"INSERT INTO shop_item (name, legacy) VALUES ('a', 0)\"),\n",
    )
    assert noops(project.check_equivalence(**NOOP)) == []


def test_run_sql(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        ITEM
        + """
        migrations.RunSQL("UPDATE shop_item SET name = 'x'"),
        migrations.RunSQL("CREATE INDEX extra_idx ON shop_item (name)"),
        """,
    )
    result = project.check_equivalence(**NOOP)
    assert len(noops(result)) == 1
    assert "UPDATE" not in project.read("shop", "0001_initial")
    assert "CREATE INDEX" in project.read("shop", "0001_initial")


def test_other_alias_kept(project: Project, tmp_path: Path) -> None:
    data_migration(
        project,
        """
        from django.db import connections
        for name in connections:
            if name != alias:
                with connections[name].cursor() as cursor:
                    cursor.execute("SELECT 1")
        """,
    )
    with temp_database(tmp_path):
        result = project.check_equivalence(**NOOP)
    assert noops(result) == []


def test_excluded_app_kept(project: Project) -> None:
    data_migration(project, "Item.objects.using(alias).update(legacy=2)")
    assert noops(project.check_equivalence(exclude=["shop"], **NOOP)) == []


def test_fold_unlocked(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_data",
        "migrations.RunPython(forwards),",
        extra="""
        def forwards(apps, schema_editor):
            alias = schema_editor.connection.alias
            apps.get_model("shop", "Item").objects.using(alias).update(name="x")
        """,
    )
    project.migration(
        "shop",
        "0003_qty",
        'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
    )
    result = project.check_equivalence(noop_data=True, skip_kinds=[])
    assert sorted(r.kind for r in result.removals) == ["fold", "noop"]


def test_plan_file(project: Project, tmp_path: Path) -> None:
    data_migration(project, "Item.objects.using(alias).update(legacy=2)")
    plan_path = tmp_path / "plan.json"
    out, _ = project.command("plan", "--noop-data", "-o", str(plan_path))
    assert "noop" in out
    data = json.loads(plan_path.read_text())
    assert data["verification"]["noop"]
    assert data["options"]["noop_data"] is True
    project.command("apply", "--plan", str(plan_path))
    assert "RunPython" not in project.read("shop", "0003_data").split("operations")[-1]


def test_needs_trial_backend(project: Project) -> None:
    data_migration(project, "pass")
    with pytest.raises(CommandError, match="can't be combined"):
        project.command("plan", "--noop-data", "--opaque", "ignore")


@pytest.mark.parametrize(
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
def test_classify(sql: str, rowcount: int | None, write: bool) -> None:
    assert (classify(sql, rowcount) is not None) is write
