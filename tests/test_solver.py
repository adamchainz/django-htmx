"""What keeps removals and superseded operations from going."""

from __future__ import annotations

import pytest

from tests.project import Project
from tests.test_compact import ITEM, blocked, kinds

ADD = 'migrations.AddField("item", "legacy", models.IntegerField(default=0)),'
INDEX = 'migrations.AddIndex("item", models.Index(fields=["legacy"], name="l_idx")),'


def unparseable(operations: str, dependency: str) -> str:
    return (
        "from django.db import migrations, models\n\n\n"
        "class Migration(migrations.Migration):\n"
        f"    dependencies = [('shop', {dependency!r})]\n"
        f"    operations = ops = [{operations}]\n"
    )


def superseded(result: object) -> list[str]:
    return sorted(
        r.label
        for r in result.removals  # type: ignore[attr-defined]
        if r.kind == "superseded"
    )


def test_unrequired_requirement_restored(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", ADD + "\n" + INDEX)
    project.migration(
        "shop",
        "0003_remove",
        """
        migrations.RunSQL("SELECT legacy FROM shop_item"),
        migrations.RemoveIndex("item", "l_idx"),
        migrations.RemoveField("item", "legacy"),
        """,
    )
    # The field can't go, so neither goes the index it required.
    result = project.compact(kinds=["field"])
    assert result.removals == []
    assert list(blocked(result)) == ["shop.Item.legacy"]


def test_elidable_hit_dropped_once(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        ADD + '\nmigrations.RunSQL("UPDATE shop_item SET legacy = 1", elidable=True),',
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )
    result = project.compact(elidable=True)
    assert ("field", "shop.Item.legacy") in kinds(result)
    assert [k for k, _ in kinds(result)].count("elidable") == 1


@pytest.mark.parametrize("policy", ["ignore", "scan"])
def test_elidable_in_excluded_app(project: Project, policy: str) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_add", ADD)
    project.migration(
        "other",
        "0001_initial",
        'migrations.RunSQL("UPDATE shop_item SET legacy = 1", elidable=True),',
        deps=[("shop", "0002_add")],
    )
    project.migration(
        "shop",
        "0003_remove",
        'migrations.RemoveField("item", "legacy"),',
        extra_deps=[("other", "0001_initial")],
    )
    result = project.compact(exclude=["other"], opaque=policy)
    if policy == "ignore":
        assert ("field", "shop.Item.legacy") in kinds(result)
    else:
        assert blocked(result)["shop.Item.legacy"] == (
            "elidable RunSQL in other.0001_initial mentions 'legacy' but cannot be"
            " removed: app 'other' is excluded",
        )


def test_poisoned_together_blocks_model(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_coupon",
        """
        migrations.CreateModel(
            name="Coupon",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("code", models.CharField(max_length=5)),
            ],
            options={"unique_together": {("id", "code")}},
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterUniqueTogether("coupon", {("id", "code")})]
        ),
        migrations.DeleteModel("Coupon"),
        """,
    )
    (reason,) = blocked(project.compact(opaque="ignore"))["shop.Coupon"]
    assert reason.startswith("its together shop.Coupon unique_together (id, code) was")


# Superseded AlterFields


def test_alter_field_in_unparseable_migration(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_a",
        source=unparseable(
            'migrations.AlterField("item", "name", models.CharField(max_length=20))',
            "0001_initial",
        ),
    )
    project.migration(
        "shop",
        "0003_b",
        'migrations.AlterField("item", "name", models.CharField(max_length=30)),',
    )
    assert superseded(project.compact()) == []


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
            'migrations.AlterField("item", "name", models.CharField(max_length=30)),',
        ),
        (
            'migrations.AlterModelOptions("item", {"verbose_name": "a"}),',
            'migrations.AlterModelOptions("item", {"verbose_name": "b"}),',
        ),
    ],
)
def test_unordered(project: Project, first: str, second: str) -> None:
    project.migration("shop", "0001_initial", ITEM)
    # Django runs the second branch first.
    project.migration("shop", "0002_b", first, deps=[("shop", "0001_initial")])
    project.migration("shop", "0002_a", second, deps=[("shop", "0001_initial")])
    project.migration(
        "shop", "0003_merge", deps=[("shop", "0002_a"), ("shop", "0002_b")]
    )
    assert superseded(project.compact()) == []


def test_many_to_many_alters_kept(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        ITEM
        + 'migrations.AddField("item", "links", models.ManyToManyField("shop.item")),',
    )
    project.migration(
        "shop",
        "0002_a",
        'migrations.AlterField("item", "links", models.ManyToManyField("shop.item", blank=True)),',
    )
    project.migration(
        "shop",
        "0003_b",
        'migrations.AlterField("item", "links", models.ManyToManyField("shop.item")),',
    )
    assert superseded(project.compact()) == []


def test_internal_type_differs(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    (project.path("shop").parent / "fields.py").write_text(
        "from django.db import models\n\n\n"
        "class Flexible(models.Field):\n"
        "    def __init__(self, *args, text=False, **kwargs):\n"
        "        self.text = text\n"
        "        super().__init__(*args, **kwargs)\n\n"
        "    def get_internal_type(self):\n"
        "        return 'TextField' if self.text else 'IntegerField'\n"
    )
    extra = f"from {project.package}.shop.fields import Flexible"
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "f", Flexible(text=True, null=True)),',
        extra=extra,
    )
    project.migration(
        "shop",
        "0003_a",
        'migrations.AlterField("item", "f", Flexible(null=True)),',
        extra=extra,
    )
    project.migration(
        "shop",
        "0004_b",
        'migrations.AlterField("item", "f", Flexible(null=True, blank=True)),',
        extra=extra,
    )
    # The second change makes the same change of type from the start.
    assert superseded(project.compact()) == ["shop.Item.f"]


# Superseded model options


def test_options_outside_alter_keys(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_a",
        'migrations.AlterModelOptions("item", {"verbose_name": "a", "db_table_comment": "c"}),',
    )
    project.migration(
        "shop",
        "0003_b",
        'migrations.AlterModelOptions("item", {"verbose_name": "b"}),',
    )
    assert superseded(project.compact()) == []


def test_options_in_unparseable_migration(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_a",
        source=unparseable(
            'migrations.AlterModelOptions("item", {"verbose_name": "a"})',
            "0001_initial",
        ),
    )
    project.migration(
        "shop",
        "0003_b",
        'migrations.AlterModelOptions("item", {"verbose_name": "b"}),',
    )
    assert superseded(project.compact()) == []


def test_index_on_two_dead_fields(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        """
        migrations.AddField("item", "a", models.IntegerField(default=0)),
        migrations.AddField("item", "b", models.IntegerField(default=0)),
        migrations.AddIndex("item", models.Index(fields=["a", "b"], name="ab_idx")),
        migrations.RunSQL("UPDATE shop_item SET a = 1, b = 2", elidable=True),
        """,
    )
    project.migration(
        "shop",
        "0003_remove",
        """
        migrations.RemoveIndex("item", "ab_idx"),
        migrations.RemoveField("item", "a"),
        migrations.RemoveField("item", "b"),
        """,
    )
    result = project.check_equivalence()
    assert [k for k, _ in kinds(result)] == ["elidable", "field", "field", "index"]


def test_superseded_with_reference_before(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        ITEM
        + 'migrations.AddIndex("item", models.Index(fields=["name"], name="n_idx")),',
    )
    project.migration(
        "shop",
        "0002_a",
        'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
    )
    project.migration(
        "shop",
        "0003_b",
        'migrations.AlterField("item", "name", models.CharField(max_length=30)),',
    )
    assert superseded(project.check_equivalence()) == ["shop.Item.name"]
