from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError
from django.db.migrations.loader import MigrationLoader

from django_clean_migrations import core
from django_clean_migrations.core import CleanError, Result
from django_clean_migrations.prune import ancestor_sets
from django_clean_migrations.verify import VerificationError
from tests.project import Project
from tests.test_clean import ITEM

PRUNE: dict[str, Any] = {"prune": True}
LEGACY = 'migrations.AddField("item", "legacy", models.IntegerField(default=0)),'
REMOVE = 'migrations.RemoveField("item", "legacy"),'
QTY = 'migrations.AddField("item", "qty", models.IntegerField(default=1)),'


def pruned(result: Result) -> list[str]:
    return [f"{a}.{n}" for a, n in result.pruned_migrations]


def prune_blocks(result: Result) -> dict[str, str]:
    return {b.label: b.reasons[0] for b in result.blocked if b.kind == "prune"}


def ancestors(project: Project) -> dict[tuple[str, str], frozenset[tuple[str, str]]]:
    project.purge()
    with project.db_blocker.unblock():
        graph = MigrationLoader(None, ignore_no_migrations=True).graph
    return ancestor_sets(graph, list(graph.nodes))


def dead_field(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", LEGACY)
    project.migration("shop", "0003_remove", REMOVE)
    project.migration("shop", "0004_qty", QTY)


def test_chain_pruned_and_rewired(project: Project) -> None:
    dead_field(project)
    project.activate()
    before = ancestors(project)
    result = project.check_equivalence(**PRUNE)
    assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]
    assert not project.path("shop", "0002_add").exists()
    assert not project.path("shop", "0003_remove").exists()
    assert '("shop", "0001_initial")' in project.read("shop", "0004_qty")
    assert "0003_remove" not in project.read("shop", "0004_qty")
    after = ancestors(project)
    gone = {("shop", "0002_add"), ("shop", "0003_remove")}
    assert after == {k: v - gone for k, v in before.items() if k not in gone}
    kinds = [(r.kind, r.start) for r in result.removals if r.kind == "prune"]
    assert kinds == [("prune", "0002_add"), ("prune", "0003_remove")]


def test_without_flag_kept(project: Project) -> None:
    dead_field(project)
    result = project.check_equivalence()
    assert pruned(result) == []
    assert project.path("shop", "0002_add").exists()


def test_leaf_and_root_kept(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        """
        migrations.CreateModel(
            name="Tmp",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
        """,
    )
    project.migration("shop", "0002_item", ITEM)
    project.migration("shop", "0003_drop", 'migrations.DeleteModel("Tmp"),')
    result = project.check_equivalence(**PRUNE)
    assert pruned(result) == []
    reasons = prune_blocks(result)
    assert reasons["shop.0001_initial"] == "it is the first migration of its app"
    assert reasons["shop.0003_drop"] == "it is the latest migration of its app"


def test_already_empty(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_nothing", "")
    project.migration("shop", "0003_qty", QTY)
    result = project.check_equivalence(**PRUNE)
    assert pruned(result) == ["shop.0002_nothing"]
    assert result.removed_operations == 0


def test_cross_app_dependant(project: Project) -> None:
    dead_field(project)
    project.migration(
        "other",
        "0001_initial",
        """
        migrations.CreateModel(
            name="Box",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
        """,
        deps=[("shop", "0003_remove")],
    )
    project.activate()
    before = ancestors(project)
    project.check_equivalence(**PRUNE)
    text = project.read("other", "0001_initial")
    assert '("shop", "0001_initial")' in text
    gone = {("shop", "0002_add"), ("shop", "0003_remove")}
    assert ancestors(project) == {
        k: v - gone for k, v in before.items() if k not in gone
    }


def test_redundant_dependency_not_added(project: Project) -> None:
    dead_field(project)
    project.migration(
        "other",
        "0001_initial",
        'migrations.RunSQL("SELECT 1"),',
        deps=[("shop", "0004_qty")],
    )
    project.migration(
        "other",
        "0002_more",
        'migrations.RunSQL("SELECT 2"),',
        extra_deps=[("shop", "0003_remove")],
    )
    project.check_equivalence(**PRUNE)
    text = project.read("other", "0002_more")
    # shop.0001 already runs before other.0001: nothing to add.
    assert "shop" not in text.split("dependencies")[1].split("]")[0]


def test_computed_dependencies_block(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", LEGACY)
    project.migration("shop", "0003_remove", REMOVE)
    project.migration(
        "shop",
        "0004_qty",
        source=f"""
        from django.db import migrations, models

        class Migration(migrations.Migration):
            dependencies = [("shop", "0003_" + "remove")]
            operations = [{QTY}]
        """,
    )
    result = project.check_equivalence(**PRUNE)
    assert pruned(result) == ["shop.0002_add"]
    assert "can't be rewritten" in prune_blocks(result)["shop.0003_remove"]


def test_first_dependency_not_copied(project: Project) -> None:
    project.migration(
        "other",
        "0001_initial",
        """
        migrations.CreateModel(
            name="Box",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
        """,
    )
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", LEGACY, extra_deps=[("other", "__first__")])
    project.migration("shop", "0003_remove", REMOVE)
    project.migration("shop", "0004_qty", QTY)
    result = project.check_equivalence(**PRUNE)
    # 0003 would need other.0001_initial, which 0002 names as __first__ (as
    # swappable_dependency() does): only 0003 goes, 0004 depends on 0002.
    assert pruned(result) == ["shop.0003_remove"]
    assert "isn't a plain tuple" in prune_blocks(result)["shop.0002_add"]
    assert '("shop", "0002_add")' in project.read("shop", "0004_qty")


def test_run_before_blocks(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", LEGACY)
    project.migration("shop", "0003_remove", REMOVE)
    project.migration("shop", "0004_qty", QTY)
    project.migration(
        "other",
        "0001_initial",
        'migrations.RunSQL("SELECT 1"),',
        extra="",
    )
    path = project.path("other", "0001_initial")
    path.write_text(
        path.read_text().replace(
            "    operations = [",
            '    run_before = [("shop", "0003_remove")]\n\n    operations = [',
        )
    )
    result = project.check_equivalence(**PRUNE)
    assert pruned(result) == ["shop.0002_add"]
    assert prune_blocks(result)["shop.0003_remove"] == (
        "other.0001_initial names it in run_before"
    )


def test_mentioned_module_blocks(project: Project) -> None:
    dead_field(project)
    project.migration(
        "other",
        "0001_initial",
        "migrations.RunPython(forwards),",
        extra="""
        import importlib


        def forwards(apps, schema_editor):
            importlib.import_module("x.shop.migrations.0002_add")
        """,
    )
    result = project.clean(**PRUNE)
    assert pruned(result) == ["shop.0003_remove"]
    assert "mentioned" in prune_blocks(result)["shop.0002_add"]


def test_squash_blocks(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", LEGACY)
    path = project.migration("shop", "0003_remove", REMOVE)
    path.write_text(
        path.read_text().replace(
            "    operations = [",
            '    replaces = [("shop", "0003_old")]\n\n    operations = [',
        )
    )
    project.migration("shop", "0004_qty", QTY)
    result = project.clean(**PRUNE)
    assert pruned(result) == ["shop.0002_add"]
    assert prune_blocks(result)["shop.0003_remove"] == "it is a squashed migration"


def test_excluded_dependant_blocks(project: Project) -> None:
    dead_field(project)
    project.migration(
        "other",
        "0001_initial",
        'migrations.RunSQL("SELECT 1"),',
        deps=[("shop", "0003_remove")],
    )
    result = project.check_equivalence(exclude=["other"], **PRUNE)
    assert "excluded app" in prune_blocks(result)["shop.0003_remove"]
    assert pruned(result) == ["shop.0002_add"]


def test_unselected_app_not_pruned(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_nothing", "")
    project.migration("shop", "0003_qty", QTY)
    project.migration("other", "0001_initial", 'migrations.RunSQL("SELECT 1"),')
    result = project.check_equivalence("other", **PRUNE)
    assert pruned(result) == []
    assert prune_blocks(result)["shop.0002_nothing"] == "app 'shop' is not selected"
    result = project.clean("shop.Item.qty", **PRUNE)
    assert pruned(result) == ["shop.0002_nothing"]


def test_plan_round_trip(project: Project, tmp_path: Path) -> None:
    dead_field(project)
    plan_path = tmp_path / "plan.json"
    out, _ = project.command("plan", "--prune", "-o", str(plan_path))
    assert "prune" in out
    assert "2 migrations would be deleted." in out
    data = json.loads(plan_path.read_text())
    deleted = [f for f in data["files"] if f["deleted"]]
    assert len(deleted) == 2
    assert all(f["content_after"] is None and f["sha256_before"] for f in deleted)
    assert data["summary"]["pruned_migrations"] == [
        ["shop", "0002_add"],
        ["shop", "0003_remove"],
    ]
    out, _ = project.command("apply", "--plan", str(plan_path))
    assert "2 migrations deleted." in out
    assert not project.path("shop", "0002_add").exists()


def test_failed_write_restores_deleted_files(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead_field(project)
    before = project.files()
    result = project.clean(**PRUNE)

    def fail(*args: Any) -> None:
        raise VerificationError("boom")

    monkeypatch.setattr(core, "verify_on_disk", fail)
    with project.db_blocker.unblock(), pytest.raises(CleanError, match="restored"):
        result.write()
    assert project.files() == before


def test_trial(project: Project) -> None:
    dead_field(project)
    result = project.check_equivalence(trial=True, **PRUNE)
    assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]
    assert result.verification["failures"] == []


def test_noop_data_then_pruned(project: Project) -> None:
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
    project.migration("shop", "0003_qty", QTY)
    result = project.check_equivalence(noop_data=True, **PRUNE)
    assert pruned(result) == ["shop.0002_data"]


def test_flag_with_plan_file(project: Project, tmp_path: Path) -> None:
    dead_field(project)
    plan_path = tmp_path / "plan.json"
    project.command("plan", "-o", str(plan_path))
    with pytest.raises(CommandError, match="--plan can't be combined"):
        project.command("apply", "--plan", str(plan_path), "--prune")
