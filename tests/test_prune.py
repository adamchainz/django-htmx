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
from unittest_parametrize import parametrize

from django_compact_migrations import core, runner
from django_compact_migrations.core import CompactError, Result
from django_compact_migrations.history import load_history
from django_compact_migrations.prune import (
    PruneError,
    PrunePlan,
    ancestor_sets,
    app_ends,
    check_ancestors,
    check_ends,
)
from django_compact_migrations.verify import VerificationError
from tests.project import ITEM, Project, ProjectTestCase

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


def plan_order(project: Project) -> list[tuple[str, str]]:
    from django_compact_migrations.history import generate_plan

    project.purge()
    with project.db_blocker.unblock():
        return generate_plan(MigrationLoader(None, ignore_no_migrations=True).graph)


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


class PruneTests(ProjectTestCase):
    def test_chain_pruned_and_rewired(self):
        dead_field(self.project)
        self.project.activate()
        before = ancestors(self.project)
        result = self.project.check_equivalence(**PRUNE)
        assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]
        assert not self.project.path("shop", "0002_add").exists()
        assert not self.project.path("shop", "0003_remove").exists()
        assert '("shop", "0001_initial")' in self.project.read("shop", "0004_qty")
        assert "0003_remove" not in self.project.read("shop", "0004_qty")
        after = ancestors(self.project)
        gone = {("shop", "0002_add"), ("shop", "0003_remove")}
        assert after == {k: v - gone for k, v in before.items() if k not in gone}
        kinds = [(r.kind, r.start) for r in result.removals if r.kind == "prune"]
        assert kinds == [("prune", "0002_add"), ("prune", "0003_remove")]

    def test_without_flag_kept(self):
        dead_field(self.project)
        result = self.project.check_equivalence()
        assert pruned(result) == []
        assert self.project.path("shop", "0002_add").exists()

    def test_leaf_and_root_kept(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Tmp",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            """,
        )
        self.project.migration("shop", "0002_item", ITEM)
        self.project.migration("shop", "0003_drop", 'migrations.DeleteModel("Tmp"),')
        result = self.project.check_equivalence(**PRUNE)
        assert pruned(result) == []
        reasons = prune_blocks(result)
        assert reasons["shop.0001_initial"] == "it is the first migration of its app"
        assert reasons["shop.0003_drop"] == "it is the latest migration of its app"

    def test_already_empty(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_nothing", "")
        self.project.migration("shop", "0003_qty", QTY)
        result = self.project.check_equivalence(**PRUNE)
        assert pruned(result) == ["shop.0002_nothing"]
        assert result.removed_operations == 0

    def test_nothing_to_prune(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_sql", 'migrations.RunSQL("SELECT 1"),')
        self.project.migration("shop", "0003_sql", 'migrations.RunSQL("SELECT 2"),')
        assert pruned(self.project.compact(**PRUNE)) == []

    def test_cross_app_dependant(self):
        dead_field(self.project)
        self.project.migration(
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
        self.project.activate()
        before = ancestors(self.project)
        self.project.check_equivalence(**PRUNE)
        text = self.project.read("other", "0001_initial")
        assert '("shop", "0001_initial")' in text
        gone = {("shop", "0002_add"), ("shop", "0003_remove")}
        assert ancestors(self.project) == {
            k: v - gone for k, v in before.items() if k not in gone
        }

    def test_redundant_dependency_not_added(self):
        dead_field(self.project)
        self.project.migration(
            "other",
            "0001_initial",
            'migrations.RunSQL("SELECT 1"),',
            deps=[("shop", "0004_qty")],
        )
        self.project.migration(
            "other",
            "0002_more",
            'migrations.RunSQL("SELECT 2"),',
            extra_deps=[("shop", "0003_remove")],
        )
        self.project.check_equivalence(**PRUNE)
        text = self.project.read("other", "0002_more")
        # shop.0001 already runs before other.0001: nothing to add.
        assert "shop" not in text.split("dependencies")[1].split("]")[0]

    def test_computed_dependencies_block(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_add", LEGACY)
        self.project.migration("shop", "0003_remove", REMOVE)
        self.project.migration(
            "shop",
            "0004_qty",
            source=f"""
            from django.db import migrations, models

            class Migration(migrations.Migration):
                dependencies = [("shop", "0003_" + "remove")]
                operations = [{QTY}]
            """,
        )
        result = self.project.check_equivalence(**PRUNE)
        assert pruned(result) == ["shop.0002_add"]
        assert "can't be rewritten" in prune_blocks(result)["shop.0003_remove"]

    def test_first_dependency_not_copied(self):
        self.project.migration(
            "other",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Box",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            """,
        )
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop", "0002_add", LEGACY, extra_deps=[("other", "__first__")]
        )
        self.project.migration("shop", "0003_remove", REMOVE)
        self.project.migration("shop", "0004_qty", QTY)
        result = self.project.check_equivalence(**PRUNE)
        # 0003 would need other.0001_initial, which 0002 names as __first__ (as
        # swappable_dependency() does): only 0003 goes, 0004 depends on 0002.
        assert pruned(result) == ["shop.0003_remove"]
        assert "isn't a plain tuple" in prune_blocks(result)["shop.0002_add"]
        assert '("shop", "0002_add")' in self.project.read("shop", "0004_qty")

    def test_run_before_blocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_add", LEGACY)
        self.project.migration("shop", "0003_remove", REMOVE)
        self.project.migration("shop", "0004_qty", QTY)
        self.project.migration(
            "other",
            "0001_initial",
            'migrations.RunSQL("SELECT 1"),',
            extra="",
        )
        path = self.project.path("other", "0001_initial")
        path.write_text(
            path.read_text().replace(
                "    operations = [",
                '    run_before = [("shop", "0003_remove")]\n\n    operations = [',
            )
        )
        result = self.project.check_equivalence(**PRUNE)
        assert pruned(result) == ["shop.0002_add"]
        assert prune_blocks(result)["shop.0003_remove"] == (
            "other.0001_initial (run_before, or replaced) refers to it"
        )

    def test_mentioned_module_blocks(self):
        dead_field(self.project)
        self.project.migration(
            "other",
            "0001_initial",
            "migrations.RunPython(forwards),",
            extra="""
            import importlib


            def forwards(apps, schema_editor):
                importlib.import_module("x.shop.migrations.0002_add")
            """,
        )
        result = self.project.compact(**PRUNE)
        assert pruned(result) == ["shop.0003_remove"]
        assert "mentioned" in prune_blocks(result)["shop.0002_add"]

    def test_dependency_of_replaced_migration_blocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_add", LEGACY)
        self.project.migration("shop", "0003_remove", REMOVE)
        self.project.migration("shop", "0004_a", QTY)
        self.project.migration("shop", "0005_b", 'migrations.RunSQL("SELECT 1"),')
        path = self.project.migration(
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
        result = self.project.compact(**PRUNE)
        # 0004_a isn't in the graph (the squash replaces it), but the loader
        # still resolves its dependency on 0003_remove.
        assert "shop.0003_remove" not in pruned(result)
        assert "replaced" in prune_blocks(result)["shop.0003_remove"]

    def test_squash_blocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_add", LEGACY)
        path = self.project.migration("shop", "0003_remove", REMOVE)
        path.write_text(
            path.read_text().replace(
                "    operations = [",
                '    replaces = [("shop", "0003_old")]\n\n    operations = [',
            )
        )
        self.project.migration("shop", "0004_qty", QTY)
        result = self.project.compact(**PRUNE)
        assert pruned(result) == ["shop.0002_add"]
        assert prune_blocks(result)["shop.0003_remove"] == "it is a squashed migration"

    def test_excluded_dependant_blocks(self):
        dead_field(self.project)
        self.project.migration(
            "other",
            "0001_initial",
            'migrations.RunSQL("SELECT 1"),',
            deps=[("shop", "0003_remove")],
        )
        result = self.project.check_equivalence(exclude=["other"], **PRUNE)
        assert "excluded app" in prune_blocks(result)["shop.0003_remove"]
        assert pruned(result) == ["shop.0002_add"]

    def test_unselected_app_not_pruned(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_nothing", "")
        self.project.migration("shop", "0003_qty", QTY)
        self.project.migration(
            "other", "0001_initial", 'migrations.RunSQL("SELECT 1"),'
        )
        result = self.project.check_equivalence("other", **PRUNE)
        assert pruned(result) == []
        assert prune_blocks(result)["shop.0002_nothing"] == "app 'shop' is not selected"
        result = self.project.compact("shop.Item.qty", **PRUNE)
        assert pruned(result) == ["shop.0002_nothing"]

    def test_plan_round_trip(self):
        dead_field(self.project)
        plan_path = self.tmp_path / "plan.toml"
        out, _ = self.project.command(
            "plan", "--static", "--prune", "-o", str(plan_path)
        )
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
        out, _ = self.project.command("apply", "--plan", str(plan_path))
        assert "2 migrations deleted." in out
        assert not self.project.path("shop", "0002_add").exists()

    def test_failed_write_restores_deleted_files(self):
        dead_field(self.project)
        before = self.project.files()
        result = self.project.compact(**PRUNE)

        def fail(*args: Any) -> None:
            raise VerificationError("boom")

        self.monkeypatch.setattr(core, "verify_on_disk", fail)
        with (
            self.project.db_blocker.unblock(),
            pytest.raises(CompactError, match="restored"),
        ):
            result.write()
        assert self.project.files() == before

    def test_trial(self):
        dead_field(self.project)
        result = self.project.check_equivalence(static=False, **PRUNE)
        assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]
        assert result.verification["failures"] == []

    def test_noop_data_then_pruned(self):
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
        self.project.migration("shop", "0003_qty", QTY)
        result = self.project.check_equivalence(noop_data=True, **PRUNE)
        assert pruned(result) == ["shop.0002_data"]

    def test_flag_with_plan_file(self):
        dead_field(self.project)
        plan_path = self.tmp_path / "plan.toml"
        self.project.command("plan", "--static", "-o", str(plan_path))
        with pytest.raises(CommandError, match="--plan can't be combined"):
            self.project.command("apply", "--plan", str(plan_path), "--prune")

    def test_same_app_dependency_kept(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "other",
            "0001_initial",
            'migrations.RunSQL("SELECT 1"),',
            deps=[("shop", "0001_initial")],
        )
        self.project.migration("shop", "0002_add", LEGACY)
        self.project.migration("shop", "0003_remove", REMOVE)
        self.project.migration(
            "shop", "0004_qty", QTY, extra_deps=[("other", "0001_initial")]
        )
        self.project.activate()
        before = leaves_and_roots(self.project)
        result = self.project.check_equivalence(**PRUNE)
        assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]
        # shop.0001 already runs before shop.0004 through other.0001, but
        # Django finds an app's latest migrations by same-app edges only.
        assert '("shop", "0001_initial")' in self.project.read("shop", "0004_qty")
        assert leaves_and_roots(self.project) == before

    def test_pruned_merge_migration(self):
        self.project.migration("shop", "0001_initial", ITEM)
        # The branches keep operations that can't go or fold anywhere.
        self.project.migration("shop", "0002_a", 'migrations.RunSQL("SELECT 1"),')
        self.project.migration(
            "shop",
            "0002_b",
            'migrations.RunSQL("SELECT 2"),',
            deps=[("shop", "0001_initial")],
        )
        self.project.migration(
            "shop", "0003_merge", "", deps=[("shop", "0002_a"), ("shop", "0002_b")]
        )
        self.project.migration("shop", "0004_more", 'migrations.RunSQL("SELECT 1"),')
        self.project.activate()
        before = leaves_and_roots(self.project)
        result = self.project.check_equivalence(**PRUNE)
        assert pruned(result) == ["shop.0003_merge"]
        text = self.project.read("shop", "0004_more")
        assert '("shop", "0002_a")' in text and '("shop", "0002_b")' in text
        assert leaves_and_roots(self.project) == before

    def test_merge_with_branch_reached_through_other_app(self):
        self.project.migration("shop", "0001_initial", ITEM)
        # The branches keep operations that can't go or fold anywhere.
        self.project.migration("shop", "0002_a", 'migrations.RunSQL("SELECT 1"),')
        self.project.migration(
            "shop",
            "0002_b",
            'migrations.RunSQL("SELECT 2"),',
            deps=[("shop", "0001_initial")],
        )
        self.project.migration(
            "other",
            "0001_initial",
            'migrations.RunSQL("SELECT 3"),',
            deps=[("shop", "0002_a")],
        )
        self.project.migration(
            "shop", "0003_merge", "", deps=[("shop", "0002_a"), ("shop", "0002_b")]
        )
        # As saleor's payment.0032: it depends on the merge and on another app's
        # migration that already runs after one branch.
        self.project.migration(
            "shop",
            "0004_more",
            'migrations.RunSQL("SELECT 2"),',
            deps=[("shop", "0003_merge"), ("other", "0001_initial")],
        )
        self.project.activate()
        before = leaves_and_roots(self.project)
        result = self.project.check_equivalence(**PRUNE)
        assert pruned(result) == ["shop.0003_merge"]
        text = self.project.read("shop", "0004_more")
        assert '("shop", "0002_a")' in text and '("shop", "0002_b")' in text
        assert leaves_and_roots(self.project) == before

    def test_order_preserved(self):
        sql = 'migrations.RunSQL("SELECT 1"),'
        self.project.migration("cc", "0001_initial", sql)
        self.project.migration("cc", "0002_more", sql)
        self.project.migration("bb", "0001_initial", sql)
        self.project.migration("aa", "0001_initial", sql)
        self.project.migration("aa", "0002_empty", "", extra_deps=[("cc", "0002_more")])
        self.project.migration(
            "aa", "0003_more", sql, extra_deps=[("bb", "0001_initial")]
        )
        self.project.activate()
        before = plan_order(self.project)
        result = self.project.check_equivalence(**PRUNE)
        # Rewired to cc.0002, aa.0003 would make migrate run cc's migrations
        # before bb.0001: kept.
        assert pruned(result) == []
        assert "order" in prune_blocks(result)["aa.0002_empty"]
        assert plan_order(self.project) == before

    def test_trial_drops_prune_when_it_fails(self):

        dead_field(self.project)

        def broken(*args: Any) -> None:
            raise PruneError("simulated")

        self.monkeypatch.setattr(runner, "check_ends", broken)
        result = self.project.check_equivalence(static=False, **PRUNE)
        assert pruned(result) == []
        assert result.verification["dropped"][0].startswith("--prune (")
        assert result.removals  # the rest of the plan stays

    def test_broken_rewiring_rolled_back(self):
        same_app_shape(self.project)
        before = self.project.files()
        result = self.project.compact(**PRUNE)

        # Only the files are wrong (the plan itself is right): the check on a
        # loader that reads them back must catch it, as migrate would.
        result.files = [
            dataclasses.replace(
                c, new=c.new.replace('        ("shop", "0001_initial"),\n', "")
            )
            for c in result.files
        ]
        with self.project.db_blocker.unblock(), pytest.raises(CompactError) as info:
            result.write()
        assert "conflicting migrations" in str(info.value)
        assert self.project.files() == before

    def test_same_app_shape_under_order_check(self):
        sql = 'migrations.RunSQL("SELECT 1"),'
        self.project.migration("cc", "0001_initial", sql)
        self.project.migration("cc", "0002_more", sql)
        self.project.migration("bb", "0001_initial", sql)
        self.project.migration("aa", "0001_initial", sql)
        self.project.migration("aa", "0002_empty", "", extra_deps=[("cc", "0002_more")])
        self.project.migration(
            "aa", "0003_more", sql, extra_deps=[("bb", "0001_initial")]
        )
        same_app_shape(self.project)
        self.project.activate()
        before_order = plan_order(self.project)
        before = leaves_and_roots(self.project)
        result = self.project.check_equivalence(**PRUNE)
        assert "order" in prune_blocks(result)["aa.0002_empty"]
        assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]
        assert '("shop", "0001_initial")' in self.project.read("shop", "0004_qty")
        assert leaves_and_roots(self.project) == before
        gone = {("shop", "0002_add"), ("shop", "0003_remove")}
        assert plan_order(self.project) == [k for k in before_order if k not in gone]

    def test_dependant_not_editable(self):
        dead_field(self.project)
        unreadable(self.monkeypatch, "0004_qty")
        result = self.project.compact(**PRUNE)
        assert prune_blocks(result)["shop.0003_remove"] == (
            "shop.0004_qty depends on it and can't be edited"
        )

    def test_empty_migration_not_editable(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_nothing")
        self.project.migration("shop", "0003_qty", QTY)
        unreadable(self.monkeypatch, "0002_nothing")
        result = self.project.compact(**PRUNE)
        assert prune_blocks(result)["shop.0002_nothing"] == (
            "it is not editable (its source can't be read)"
        )

    def test_own_run_before_blocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "other", "0001_initial", 'migrations.RunSQL("SELECT 1"),'
        )
        self.project.migration(
            "shop",
            "0002_nothing",
            source=source(
                'dependencies = [("shop", "0001_initial")]\n'
                '    run_before = [("other", "0001_initial")]'
            ),
        )
        self.project.migration("shop", "0003_qty", QTY)
        result = self.project.compact(**PRUNE)
        assert prune_blocks(result)["shop.0002_nothing"] == "it has run_before"

    def test_pruned_diamond(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop", "0002_a", LEGACY, deps=[("shop", "0001_initial")]
        )
        self.project.migration(
            "shop",
            "0002_b",
            'migrations.AddField("item", "other", models.IntegerField(default=0)),',
            deps=[("shop", "0001_initial")],
        )
        self.project.migration(
            "shop", "0003_merge", deps=[("shop", "0002_a"), ("shop", "0002_b")]
        )
        self.project.migration(
            "shop",
            "0004_qty",
            REMOVE + '\nmigrations.RemoveField("item", "other"),\n' + QTY,
        )
        result = self.project.check_equivalence(**PRUNE)
        assert pruned(result) == ["shop.0002_b", "shop.0002_a", "shop.0003_merge"]
        assert '("shop", "0001_initial")' in self.project.read("shop", "0004_qty")

    def test_unreadable_file_not_scanned(self):
        dead_field(self.project)
        unreadable(self.monkeypatch, "0001_initial")
        result = self.project.compact(**PRUNE)
        assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]

    def test_mentioned_by_module_without_class(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_add", LEGACY)
        self.project.migration("shop", "0003_remove", REMOVE)
        self.project.migration(
            "shop",
            "0004_qty",
            source="from django.db import migrations, models\n\n"
            "Migration = type(\n"
            '    "Migration",\n'
            "    (migrations.Migration,),\n"
            f'    {{"dependencies": [("shop", "0003_remove")], "operations": [{QTY}]}},\n'
            ")\n",
        )
        result = self.project.compact(**PRUNE)
        assert "mentioned" in prune_blocks(result)["shop.0003_remove"]

    @parametrize(
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
    def test_dependency_forms(self, dependencies, rewritten):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_add", LEGACY)
        self.project.migration("shop", "0003_remove", REMOVE)
        self.project.migration(
            "shop",
            "0004_qty",
            source=source(
                dependencies, QTY, head='DEPS = [("shop", "0003_" + "remove")]\n'
            ),
        )
        result = self.project.compact(**PRUNE)
        if rewritten is None:
            assert "can't be rewritten" in prune_blocks(result)["shop.0003_remove"]
        else:
            result.write()
            assert rewritten in self.project.read("shop", "0004_qty")

    def test_invariant_failures(self):
        dead_field(self.project)
        self.project.migration("other", "0001_initial", deps=[("shop", "0001_initial")])
        self.project.activate()
        with self.project.db_blocker.unblock():
            history = load_history()
        graph = history.loader.graph
        remove = ("shop", "0003_remove")
        plan = PrunePlan(pruned=[remove], parents={("shop", "0004_qty"): [remove]})
        with pytest.raises(PruneError, match="still depends on pruned"):
            check_ancestors(history, plan)
        other = ("other", "0001_initial")
        plan = PrunePlan(
            pruned=[remove],
            parents={("shop", "0004_qty"): [("shop", "0002_add"), other]},
        )
        with pytest.raises(
            PruneError, match="would change the migrations shop.0004_qty"
        ):
            check_ancestors(history, plan)
        empty = MigrationGraph()
        with pytest.raises(PruneError, match="latest migrations of other"):
            check_ends(app_ends(graph), empty, [])
        only_leaf = MigrationGraph()
        only_leaf.add_node(("shop", "0004_qty"), None)
        only_leaf.add_node(other, None)
        with pytest.raises(PruneError, match="first migrations of shop"):
            check_ends(app_ends(graph), only_leaf, [])
        keys = [("shop", "0004_qty"), ("shop", "0001_initial")]
        assert ancestor_sets(graph, keys)[("shop", "0001_initial")] == frozenset()

    def test_ancestor_sets_redundant_dependency(self):
        graph = MigrationGraph()
        a, b, c = ("x", "0001"), ("x", "0002"), ("x", "0003")
        for key in (a, b, c):
            graph.add_node(key, None)
        graph.add_dependency(None, b, a)
        graph.add_dependency(None, c, a)
        graph.add_dependency(None, c, b)
        assert ancestor_sets(graph, [c])[c] == {a, b}

    def test_dependant_already_after_parent(self):
        dead_field(self.project)
        path = self.project.path("shop", "0004_qty")
        path.write_text(
            path.read_text().replace(
                "('shop', '0003_remove'),",
                "('shop', '0003_remove'),\n        ('shop', '0001_initial'),",
            )
        )
        result = self.project.check_equivalence(**PRUNE)
        assert pruned(result) == ["shop.0002_add", "shop.0003_remove"]
        text = self.project.read("shop", "0004_qty")
        assert "0001_initial" in text and "0003_remove" not in text
        assert text.count("0001_initial") == 1

    def test_prune_independent_of_hash_seed(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_q", 'migrations.RunSQL("SELECT 1"),')
        self.project.migration(
            "shop", "0003_p", deps=[("shop", "0002_q"), ("shop", "0001_initial")]
        )
        self.project.migration("shop", "0004_last", 'migrations.RunSQL("SELECT 1"),')
        self.project.migration(
            "other", "0001_initial", 'migrations.RunSQL("SELECT 1"),'
        )
        self.project.migration(
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
                        str(self.project.root),
                        self.project.package,
                    ],
                    env={**os.environ, "PYTHONHASHSEED": seed},
                    cwd=self.project.root,  # the project root: its migrations are edited
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
        (output,) = outputs
        result = json.loads(output)
        assert result["pruned"] == ["shop.0003_p"]
        dependencies = (
            result["files"]["0002_c"].split("dependencies = [")[1].split("]")[0]
        )
        assert [line.strip() for line in dependencies.strip().splitlines()] == [
            "('other', '0001_initial'),",
            '("shop", "0002_q"),',
        ]
