from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.loader import MigrationLoader

from django_compact_migrations import core
from django_compact_migrations.core import CompactError, Result
from django_compact_migrations.history import load_history
from django_compact_migrations.prune import (
    PruneError,
    PrunePlan,
    ancestor_sets,
    check_ancestors,
    check_ends,
)
from django_compact_migrations.verify import VerificationError
from tests.project import Project
from tests.test_compact import ITEM, blocked

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
        "other.0001_initial (run_before, or replaced) refers to it"
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
    result = project.compact(**PRUNE)
    assert pruned(result) == ["shop.0003_remove"]
    assert "mentioned" in prune_blocks(result)["shop.0002_add"]


def test_dependency_of_replaced_migration_blocks(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", LEGACY)
    project.migration("shop", "0003_remove", REMOVE)
    project.migration("shop", "0004_a", QTY)
    project.migration("shop", "0005_b", 'migrations.RunSQL("SELECT 1"),')
    path = project.migration(
        "shop",
        "0004_a_squashed_0005_b",
        QTY + '\nmigrations.RunSQL("SELECT 1"),',
        deps=[("shop", "0003_remove")],
    )
    path.write_text(
        path.read_text().replace(
            "    operations = [",
            '    replaces = [("shop", "0004_a"), ("shop", "0005_b")]\n\n'
            "    operations = [",
        )
    )
    result = project.compact(**PRUNE)
    # 0004_a isn't in the graph (the squash replaces it), but the loader
    # still resolves its dependency on 0003_remove.
    assert "shop.0003_remove" not in pruned(result)
    assert "replaced" in prune_blocks(result)["shop.0003_remove"]


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
    result = project.compact(**PRUNE)
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
    result = project.compact("shop.Item.qty", **PRUNE)
    assert pruned(result) == ["shop.0002_nothing"]


def test_plan_round_trip(project: Project, tmp_path: Path) -> None:
    dead_field(project)
    plan_path = tmp_path / "plan.toml"
    out, _ = project.command("plan", "--prune", "-o", str(plan_path))
    assert "prune" in out
    assert "2 migrations would be deleted." in out
    data = tomllib.loads(plan_path.read_text())
    deleted = data["deleted"]
    assert len(deleted) == 2
    assert all(f["sha256_before"] and "diff" not in f for f in deleted)
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
    result = project.compact(**PRUNE)

    def fail(*args: Any) -> None:
        raise VerificationError("boom")

    monkeypatch.setattr(core, "verify_on_disk", fail)
    with project.db_blocker.unblock(), pytest.raises(CompactError, match="restored"):
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
    plan_path = tmp_path / "plan.toml"
    project.command("plan", "-o", str(plan_path))
    with pytest.raises(CommandError, match="--plan can't be combined"):
        project.command("apply", "--plan", str(plan_path), "--prune")


def leaves_and_roots(project: Project) -> dict[str, Any]:
    project.purge()
    with project.db_blocker.unblock():
        loader = MigrationLoader(None, ignore_no_migrations=True)
    assert loader.detect_conflicts() == {}
    return {
        app: (
            sorted(loader.graph.root_nodes(app)),
            sorted(loader.graph.leaf_nodes(app)),
        )
        for app in ("shop", "other")
    }


def test_same_app_dependency_kept(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "other",
        "0001_initial",
        'migrations.RunSQL("SELECT 1"),',
        deps=[("shop", "0001_initial")],
    )
    project.migration("shop", "0002_add", LEGACY)
    project.migration("shop", "0003_remove", REMOVE)
    project.migration("shop", "0004_qty", QTY, extra_deps=[("other", "0001_initial")])
    project.activate()
    before = leaves_and_roots(project)
    result = project.check_equivalence(**PRUNE)
    assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]
    # shop.0001 already runs before shop.0004 through other.0001, but
    # Django finds an app's latest migrations by same-app edges only.
    assert '("shop", "0001_initial")' in project.read("shop", "0004_qty")
    assert leaves_and_roots(project) == before


def test_pruned_merge_migration(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_a", QTY)
    project.migration(
        "shop",
        "0002_b",
        'migrations.AddField("item", "size", models.IntegerField(default=1)),',
        deps=[("shop", "0001_initial")],
    )
    project.migration(
        "shop", "0003_merge", "", deps=[("shop", "0002_a"), ("shop", "0002_b")]
    )
    project.migration("shop", "0004_more", 'migrations.RunSQL("SELECT 1"),')
    project.activate()
    before = leaves_and_roots(project)
    result = project.check_equivalence(**PRUNE)
    assert pruned(result) == ["shop.0003_merge"]
    text = project.read("shop", "0004_more")
    assert '("shop", "0002_a")' in text and '("shop", "0002_b")' in text
    assert leaves_and_roots(project) == before


def test_merge_with_branch_reached_through_other_app(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_a", QTY)
    project.migration(
        "shop",
        "0002_b",
        'migrations.AddField("item", "size", models.IntegerField(default=1)),',
        deps=[("shop", "0001_initial")],
    )
    project.migration(
        "other",
        "0001_initial",
        'migrations.RunSQL("SELECT 1"),',
        deps=[("shop", "0002_a")],
    )
    project.migration(
        "shop", "0003_merge", "", deps=[("shop", "0002_a"), ("shop", "0002_b")]
    )
    # As saleor's payment.0032: it depends on the merge and on another app's
    # migration that already runs after one branch.
    project.migration(
        "shop",
        "0004_more",
        'migrations.RunSQL("SELECT 2"),',
        deps=[("shop", "0003_merge"), ("other", "0001_initial")],
    )
    project.activate()
    before = leaves_and_roots(project)
    result = project.check_equivalence(**PRUNE)
    assert pruned(result) == ["shop.0003_merge"]
    text = project.read("shop", "0004_more")
    assert '("shop", "0002_a")' in text and '("shop", "0002_b")' in text
    assert leaves_and_roots(project) == before


def plan_order(project: Project) -> list[tuple[str, str]]:
    from django_compact_migrations.history import generate_plan

    project.purge()
    with project.db_blocker.unblock():
        return generate_plan(MigrationLoader(None, ignore_no_migrations=True).graph)


def test_order_preserved(project: Project) -> None:
    sql = 'migrations.RunSQL("SELECT 1"),'
    project.migration("cc", "0001_initial", sql)
    project.migration("cc", "0002_more", sql)
    project.migration("bb", "0001_initial", sql)
    project.migration("aa", "0001_initial", sql)
    project.migration("aa", "0002_empty", "", extra_deps=[("cc", "0002_more")])
    project.migration("aa", "0003_more", sql, extra_deps=[("bb", "0001_initial")])
    project.activate()
    before = plan_order(project)
    result = project.check_equivalence(**PRUNE)
    # Rewired to cc.0002, aa.0003 would make migrate run cc's migrations
    # before bb.0001: kept.
    assert pruned(result) == []
    assert "order" in prune_blocks(result)["aa.0002_empty"]
    assert plan_order(project) == before


def test_trial_drops_prune_when_it_fails(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    from django_compact_migrations import runner
    from django_compact_migrations.prune import PruneError

    dead_field(project)

    def broken(*args: Any) -> None:
        raise PruneError("simulated")

    monkeypatch.setattr(runner, "check_ends", broken)
    result = project.check_equivalence(trial=True, **PRUNE)
    assert pruned(result) == []
    assert result.verification["dropped"][0].startswith("--prune (")
    assert result.removals  # the rest of the plan stays


def test_trial_blames_until_a_plan_passes(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    from django.db import migrations

    from django_compact_migrations import trial as trial_module

    dead_field(project)

    def sabotage(operations: dict[Any, list[Any]], passes: int) -> None:
        for ops in operations.values():
            ops.append(migrations.RunSQL("SELECT * FROM nowhere"))

    monkeypatch.setattr(trial_module, "_candidate_hook", sabotage)
    # Every edited migration fails: blamed until nothing is edited.
    result = project.compact(trial=True, trial_max_runs=1, **PRUNE)
    assert result.removals == []
    assert result.verification["dropped"] == []
    assert "no such table: nowhere" in blocked(result)["shop.Item.legacy"][0]


def same_app_shape(project: Project) -> None:
    """pretalx's common app: 0004 depended on the pruned 0003 and on
    another app that already runs after shop.0001."""
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "other",
        "0001_initial",
        'migrations.RunSQL("SELECT 1"),',
        deps=[("shop", "0001_initial")],
    )
    project.migration("shop", "0002_add", LEGACY)
    project.migration("shop", "0003_remove", REMOVE)
    project.migration("shop", "0004_qty", QTY, extra_deps=[("other", "0001_initial")])


def test_broken_rewiring_rolled_back(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    same_app_shape(project)
    before = project.files()
    result = project.compact(**PRUNE)

    # Only the files are wrong (the plan itself is right): the check on a
    # loader that reads them back must catch it, as migrate would.
    result.files = [
        dataclasses.replace(
            c, new=c.new.replace('        ("shop", "0001_initial"),\n', "")
        )
        for c in result.files
    ]
    with project.db_blocker.unblock(), pytest.raises(CompactError) as info:
        result.write()
    assert "conflicting migrations" in str(info.value)
    assert project.files() == before


def test_same_app_shape_under_order_check(project: Project) -> None:
    # The aa/bb/cc part makes the first plan change migrate's order, so
    # pruning goes one migration at a time; the shop part must still keep
    # its same-app dependency.
    sql = 'migrations.RunSQL("SELECT 1"),'
    project.migration("cc", "0001_initial", sql)
    project.migration("cc", "0002_more", sql)
    project.migration("bb", "0001_initial", sql)
    project.migration("aa", "0001_initial", sql)
    project.migration("aa", "0002_empty", "", extra_deps=[("cc", "0002_more")])
    project.migration("aa", "0003_more", sql, extra_deps=[("bb", "0001_initial")])
    same_app_shape(project)
    project.activate()
    before_order = plan_order(project)
    before = leaves_and_roots(project)
    result = project.check_equivalence(**PRUNE)
    assert "order" in prune_blocks(result)["aa.0002_empty"]
    assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]
    assert '("shop", "0001_initial")' in project.read("shop", "0004_qty")
    assert leaves_and_roots(project) == before
    gone = {("shop", "0002_add"), ("shop", "0003_remove")}
    assert plan_order(project) == [k for k in before_order if k not in gone]


def source(dependencies: str, operations: str = "", head: str = "") -> str:
    return (
        "from django.db import migrations, models\n\n"
        + head
        + "\n\nclass Migration(migrations.Migration):\n"
        '    """Written by hand."""\n\n'
        f"    {dependencies}\n"
        f"    operations = [{operations}]\n"
    )


def unreadable(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    from django_compact_migrations import history

    real = history._read_source

    def read(path: Path) -> Any:
        return None if path.name == f"{name}.py" else real(path)

    monkeypatch.setattr(history, "_read_source", read)


def test_dependant_not_editable(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead_field(project)
    unreadable(monkeypatch, "0004_qty")
    result = project.compact(**PRUNE)
    assert prune_blocks(result)["shop.0003_remove"] == (
        "shop.0004_qty depends on it and can't be edited"
    )


def test_empty_migration_not_editable(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_nothing")
    project.migration("shop", "0003_qty", QTY)
    unreadable(monkeypatch, "0002_nothing")
    result = project.compact(**PRUNE)
    assert prune_blocks(result)["shop.0002_nothing"] == (
        "it is not editable (installed package)"
    )


def test_own_run_before_blocks(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("other", "0001_initial", 'migrations.RunSQL("SELECT 1"),')
    project.migration(
        "shop",
        "0002_nothing",
        source=source(
            'dependencies = [("shop", "0001_initial")]\n'
            '    run_before = [("other", "0001_initial")]'
        ),
    )
    project.migration("shop", "0003_qty", QTY)
    result = project.compact(**PRUNE)
    assert prune_blocks(result)["shop.0002_nothing"] == "it has run_before"


def test_pruned_diamond(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_a", LEGACY, deps=[("shop", "0001_initial")])
    project.migration(
        "shop",
        "0002_b",
        'migrations.AddField("item", "other", models.IntegerField(default=0)),',
        deps=[("shop", "0001_initial")],
    )
    project.migration(
        "shop", "0003_merge", deps=[("shop", "0002_a"), ("shop", "0002_b")]
    )
    project.migration(
        "shop",
        "0004_qty",
        REMOVE + '\nmigrations.RemoveField("item", "other"),\n' + QTY,
    )
    result = project.check_equivalence(**PRUNE)
    assert pruned(result) == ["shop.0002_b", "shop.0002_a", "shop.0003_merge"]
    assert '("shop", "0001_initial")' in project.read("shop", "0004_qty")


def test_unreadable_file_not_scanned(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead_field(project)
    unreadable(monkeypatch, "0001_initial")
    result = project.compact(**PRUNE)
    assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]


def test_mentioned_by_module_without_class(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", LEGACY)
    project.migration("shop", "0003_remove", REMOVE)
    project.migration(
        "shop",
        "0004_qty",
        source="from django.db import migrations, models\n\n"
        "Migration = type(\n"
        '    "Migration",\n'
        "    (migrations.Migration,),\n"
        f'    {{"dependencies": [("shop", "0003_remove")], "operations": [{QTY}]}},\n'
        ")\n",
    )
    result = project.compact(**PRUNE)
    assert "mentioned" in prune_blocks(result)["shop.0003_remove"]


@pytest.mark.parametrize(
    ("dependencies", "rewritten"),
    [
        (
            'dependencies: list = [("shop", "0003_remove")]',
            'dependencies: list = [("shop", "0001_initial")]',
        ),
        (
            'dependencies = (("shop", "0003_remove"),)',
            'dependencies = (("shop", "0001_initial"),)',
        ),
        ("dependencies = DEPS", None),
    ],
)
def test_dependency_forms(
    project: Project, dependencies: str, rewritten: str | None
) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", LEGACY)
    project.migration("shop", "0003_remove", REMOVE)
    project.migration(
        "shop",
        "0004_qty",
        source=source(
            dependencies, QTY, head='DEPS = [("shop", "0003_" + "remove")]\n'
        ),
    )
    result = project.compact(**PRUNE)
    if rewritten is None:
        assert "can't be rewritten" in prune_blocks(result)["shop.0003_remove"]
    else:
        result.write()
        assert rewritten in project.read("shop", "0004_qty")


def test_invariant_failures(project: Project) -> None:
    dead_field(project)
    project.migration("other", "0001_initial", deps=[("shop", "0001_initial")])
    project.activate()
    with project.db_blocker.unblock():
        history = load_history()
    graph = history.loader.graph
    remove = ("shop", "0003_remove")
    plan = PrunePlan(pruned=[remove], parents={("shop", "0004_qty"): [remove]})
    with pytest.raises(PruneError, match="still depends on pruned"):
        check_ancestors(history, plan)
    other = ("other", "0001_initial")
    plan = PrunePlan(
        pruned=[remove], parents={("shop", "0004_qty"): [("shop", "0002_add"), other]}
    )
    with pytest.raises(PruneError, match="would change the migrations shop.0004_qty"):
        check_ancestors(history, plan)
    empty = MigrationGraph()
    with pytest.raises(PruneError, match="latest migrations of other"):
        check_ends(graph, empty, [])
    only_leaf = MigrationGraph()
    only_leaf.add_node(("shop", "0004_qty"), None)
    only_leaf.add_node(other, None)
    with pytest.raises(PruneError, match="first migrations of shop"):
        check_ends(graph, only_leaf, [])
    keys = [("shop", "0004_qty"), ("shop", "0001_initial")]
    assert ancestor_sets(graph, keys)[("shop", "0001_initial")] == frozenset()


def test_ancestor_sets_redundant_dependency() -> None:
    graph = MigrationGraph()
    a, b, c = ("x", "0001"), ("x", "0002"), ("x", "0003")
    for key in (a, b, c):
        graph.add_node(key, None)
    graph.add_dependency(None, b, a)
    graph.add_dependency(None, c, a)
    graph.add_dependency(None, c, b)
    assert ancestor_sets(graph, [c])[c] == {a, b}


def test_dependant_already_after_parent(project: Project) -> None:
    dead_field(project)
    path = project.path("shop", "0004_qty")
    path.write_text(
        path.read_text().replace(
            "('shop', '0003_remove'),",
            "('shop', '0003_remove'),\n        ('shop', '0001_initial'),",
        )
    )
    result = project.check_equivalence(**PRUNE)
    assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]
    text = project.read("shop", "0004_qty")
    assert "0001_initial" in text and "0003_remove" not in text
    assert text.count("0001_initial") == 1


PRUNE_IN_SUBPROCESS = """
import json, sys
import django
from django.conf import settings

sys.path.insert(0, sys.argv[1])
settings.configure(
    INSTALLED_APPS=[f"{sys.argv[2]}.shop", f"{sys.argv[2]}.other"],
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
)
django.setup()
from django_compact_migrations.core import plan

result = plan(prune=True)
print(json.dumps({
    "pruned": sorted(".".join(k) for k in result.pruned_migrations),
    "files": {str(c.migration[1]): c.new for c in result.files if not c.deleted},
}))
"""


def test_prune_independent_of_hash_seed(project: Project) -> None:
    # other.0002_c depends on the empty shop.0003_p, which depends on both
    # shop.0001 and shop.0002 (itself after shop.0001). Inheriting both is
    # redundant: walked latest first, only shop.0002 is added; Django's
    # node sets used to give either, depending on string hashing.
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_q", 'migrations.RunSQL("SELECT 1"),')
    project.migration(
        "shop", "0003_p", deps=[("shop", "0002_q"), ("shop", "0001_initial")]
    )
    project.migration("shop", "0004_last", 'migrations.RunSQL("SELECT 1"),')
    project.migration("other", "0001_initial", 'migrations.RunSQL("SELECT 1"),')
    project.migration(
        "other",
        "0002_c",
        'migrations.RunSQL("SELECT 2"),',
        deps=[("other", "0001_initial"), ("shop", "0003_p")],
    )
    outputs = set()
    for seed in ("0", "1", "2", "3", "123"):
        outputs.add(
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    PRUNE_IN_SUBPROCESS,
                    str(project.root),
                    project.package,
                ],
                env={**os.environ, "PYTHONHASHSEED": seed},
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
    (output,) = outputs
    result = json.loads(output)
    assert result["pruned"] == ["shop.0003_p"]
    dependencies = result["files"]["0002_c"].split("dependencies = [")[1].split("]")[0]
    assert [line.strip() for line in dependencies.strip().splitlines()] == [
        "('other', '0001_initial'),",
        '("shop", "0002_q"),',
    ]
