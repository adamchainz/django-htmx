from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from django.db.migrations.operations.base import Operation

from django_clean_migrations import trial as trial_module
from django_clean_migrations.core import CleanError
from tests.project import Project
from tests.test_clean import ITEM, blocked, kinds

LEGACY = 'migrations.AddField("item", "legacy", models.IntegerField(default=0)),'


def history(project: Project, code: str, extra_ops: str = "") -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        LEGACY + "\n" + extra_ops + "\nmigrations.RunPython(forwards),",
        extra="def forwards(apps, schema_editor):\n"
        "    Item = apps.get_model('shop', 'Item')\n"
        "    alias = schema_editor.connection.alias\n"
        + "\n".join(f"    {line}" for line in code.strip().splitlines()),
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )


def test_mention_without_effect_is_unlocked(project: Project) -> None:
    history(
        project,
        """
        for item in Item.objects.using(alias).all():
            item.legacy = 1
            item.save()
        """,
    )
    assert project.clean().removals == []
    result = project.check_equivalence(trial=True)
    assert kinds(result) == [("field", "shop.Item.legacy")]
    info = result.verification
    assert info["mode"] == "trial"
    assert info["vendor"] == "sqlite"
    assert info["unlocked"] == 1
    assert info["passes"] == 1
    assert info["failures"] == []


def test_failure_harvested_and_blocked(project: Project) -> None:
    history(
        project,
        "Item.objects.using(alias).filter(legacy=1).count()",
        extra_ops="""
        migrations.CreateModel(
            name="Coupon",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
        migrations.RunSQL("SELECT 'coupon'"),
        migrations.DeleteModel("Coupon"),
        """,
    )
    result = project.check_equivalence(trial=True)
    assert kinds(result) == [("model", "shop.Coupon")]
    (reason,) = blocked(result)["shop.Item.legacy"]
    assert reason.startswith(
        "trial migrate failed in shop.0002_add RunPython: FieldError: Cannot"
        " resolve keyword 'legacy'"
    )
    info = result.verification
    assert info["passes"] == 2
    assert info["trial_blocked"] == 1
    assert [f["harvested"] for f in info["failures"]] == [True]


def test_seeded_rows(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_seed",
        """
        migrations.AddField("item", "legacy", models.IntegerField(default=0)),
        migrations.AddField("item", "spare", models.IntegerField(default=0)),
        migrations.RunPython(seed),
        """,
        extra="""
        def seed(apps, schema_editor):
            Item = apps.get_model("shop", "Item")
            alias = schema_editor.connection.alias
            Item.objects.using(alias).create(name="a", legacy=5)
            Item.objects.using(alias).create(name="spare-free")
        """,
    )
    project.migration(
        "shop",
        "0003_remove",
        """
        migrations.RemoveField("item", "legacy"),
        migrations.RemoveField("item", "spare"),
        """,
    )
    result = project.check_equivalence(trial=True)
    # "spare" is only mentioned through its name in the seed function's
    # source; seeding works without it and leaves identical rows.
    assert kinds(result) == [("field", "shop.Item.spare")]
    assert blocked(result)["shop.Item.legacy"][0].startswith(
        "trial migrate failed in shop.0002_seed RunPython:"
    )


def test_soft_reference_crash_blocked_by_trial(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        """
        migrations.CreateModel(
            name="Item",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("name", models.CharField(max_length=10)),
            ],
            options={"ordering": ("_name", "pk")},
        ),
        """,
    )
    project.migration(
        "shop",
        "0002_name",
        """
        migrations.AddField("item", "_name", models.CharField(default="", max_length=10)),
        migrations.RunPython(forwards),
        """,
        extra="""
        def forwards(apps, schema_editor):
            alias = schema_editor.connection.alias
            list(apps.get_model("shop", "Item").objects.using(alias).all())
        """,
    )
    project.migration(
        "shop",
        "0003_natural",
        """
        migrations.AlterModelOptions("item", {"ordering": ("name", "pk")}),
        migrations.RemoveField("item", "_name"),
        """,
    )
    result = project.check_equivalence(trial=True)
    assert result.removals == []
    (reason,) = blocked(result)["shop.Item._name"]
    assert "FieldError: Cannot resolve keyword '_name'" in reason


@pytest.fixture
def break_index_once() -> Iterator[None]:
    def hook(operations: dict[tuple[str, str], list[Operation]], n: int) -> None:
        if n == 0:
            from django.db.migrations.loader import MigrationLoader

            loader = MigrationLoader(None, ignore_no_migrations=True)
            key = next(k for k in loader.graph.nodes if k[1] == "0004_index")
            ops = operations.get(key, loader.graph.nodes[key].operations)
            operations[key] = [o for o in ops if type(o).__name__ != "AddIndex"]

    trial_module._candidate_hook = hook
    try:
        yield
    finally:
        trial_module._candidate_hook = None


@pytest.mark.usefixtures("break_index_once")
def test_schema_difference_detected(project: Project) -> None:
    # The mention makes legacy's removal depend on the trial (the static plan
    # keeps it); removals the static plan makes are never blamed.
    history(project, 'Item.objects.using(alias).count()\nunused = "legacy"')
    project.migration(
        "shop",
        "0004_index",
        'migrations.AddIndex("item", models.Index(fields=["name"], name="name_idx")),',
    )
    result = project.check_equivalence(trial=True)
    assert result.removals == []
    (reason,) = blocked(result)["shop.Item.legacy"]
    assert reason == (
        "trial: schema of table shop_item differs (index sql name_idx missing)"
    )


def test_baseline_failure(project: Project) -> None:
    project.migration(
        "shop", "0001_initial", "migrations.RunSQL('SELECT * FROM nope'),"
    )
    with pytest.raises(CleanError, match="original migration history doesn't migrate"):
        project.clean(trial=True)


def test_plan_file_round_trip(project: Project, tmp_path: Path) -> None:
    history(
        project,
        """
        for item in Item.objects.using(alias).all():
            item.legacy = 1
        """,
    )
    plan_path = tmp_path / "plan.json"
    out, _ = project.command("plan", "--trial", "-o", str(plan_path))
    assert "Trial: 2 migrate runs on sqlite in " in out
    assert "unlocked 1 removal that the static scan blocks" in out
    data = json.loads(plan_path.read_text())
    assert data["verification"]["mode"] == "trial"
    assert data["verification"]["unlocked"] == 1
    assert data["options"]["targets"] == []
    project.command("apply", "--plan", str(plan_path))
    assert 'AddField("item", "legacy"' not in project.read("shop", "0002_add")


def test_trial_with_opaque_policy(project: Project) -> None:
    history(project, "pass")
    from django.core.management import CommandError

    with pytest.raises(CommandError, match="--trial replaces the --opaque"):
        project.command("plan", "--trial", "--opaque", "block")


def test_non_atomic_failure_ends_pass(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        source="""
        from django.db import migrations, models


        def forwards(apps, schema_editor):
            alias = schema_editor.connection.alias
            apps.get_model("shop", "Item").objects.using(alias).filter(legacy=1).count()


        class Migration(migrations.Migration):
            atomic = False
            dependencies = [("shop", "0001_initial")]
            operations = [
                migrations.AddField("item", "legacy", models.IntegerField(default=0)),
                migrations.RunPython(forwards),
            ]
        """,
    )
    project.migration(
        "shop",
        "0003_remove",
        'migrations.RemoveField("item", "legacy"),',
        deps=[("shop", "0002_add")],
    )
    result = project.check_equivalence(trial=True)
    assert result.removals == []
    assert [f["harvested"] for f in result.verification["failures"]] == [False]
    assert blocked(result)["shop.Item.legacy"][0].startswith(
        "trial migrate failed in shop.0002_add RunPython: FieldError"
    )
