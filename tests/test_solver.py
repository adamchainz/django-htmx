"""What keeps removals and superseded operations from going."""

from __future__ import annotations

import pytest

from tests.project import ITEM, blocked, kinds, only

ADD = 'migrations.AddField("item", "legacy", models.IntegerField(default=0)),'
INDEX = 'migrations.AddIndex("item", models.Index(fields=["legacy"], name="l_idx")),'


def unparsable(operations: str, dependency: str) -> str:
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


def test_unrequired_requirement_restored(project):
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
    result = project.compact(**only("field"))
    assert result.removals == []
    assert list(blocked(result)) == ["shop.Item.legacy"]


def test_elidable_hit_dropped_once(project):
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


def test_elidable_in_excluded_app(project):
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
    result = project.compact(exclude=["other"])
    assert blocked(result)["shop.Item.legacy"] == (
        "elidable RunSQL in other.0001_initial mentions 'legacy' but cannot be"
        " removed: app 'other' is excluded",
    )


def test_poisoned_together_blocks_model(project):
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
    (reason,) = blocked(project.compact())["shop.Coupon"]
    assert reason.startswith("its together shop.Coupon unique_together (id, code) was")


# Superseded AlterFields


def test_alter_field_in_unparsable_migration(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_a",
        source=unparsable(
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
def test_unordered(project, first, second):
    project.migration("shop", "0001_initial", ITEM)
    # Django runs the second branch first.
    project.migration("shop", "0002_b", first, deps=[("shop", "0001_initial")])
    project.migration("shop", "0002_a", second, deps=[("shop", "0001_initial")])
    project.migration(
        "shop", "0003_merge", deps=[("shop", "0002_a"), ("shop", "0002_b")]
    )
    assert superseded(project.compact()) == []


def test_many_to_many_alters_kept(project):
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


def test_internal_type_differs(project):
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


def test_options_outside_alter_keys(project):
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


def test_options_in_unparsable_migration(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_a",
        source=unparsable(
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


def test_index_on_two_dead_fields(project):
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


def test_superseded_with_reference_before(project):
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


TAG = """
    migrations.CreateModel(
        name="Tag",
        fields=[
            ("id", models.AutoField(primary_key=True)),
            ("code", models.CharField(max_length=5, unique=True)),
        ],
    ),
"""


class TestSupersededReleases:
    def test_to_field_released(self, project):
        project.migration("shop", "0001_initial", ITEM + TAG)
        project.migration(
            "shop",
            "0003_tag",
            'migrations.AddField("item", "tag", models.ForeignKey("shop.tag",'
            ' models.CASCADE, to_field="code", null=True)),',
        )
        project.migration(
            "shop",
            "0004_alter",
            'migrations.AlterField("item", "tag", models.ForeignKey("shop.tag",'
            " models.CASCADE, null=True)),",
        )
        project.migration(
            "shop", "0005_drop_code", 'migrations.RemoveField("tag", "code"),'
        )
        project.migration(
            "shop",
            "0006_blank",
            'migrations.AlterField("item", "tag", models.ForeignKey("shop.tag",'
            " models.CASCADE, null=True, blank=True)),",
        )
        result = project.check_equivalence()
        assert result.removals == []

    def test_db_column_released(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            'migrations.AddField("item", "a", models.IntegerField(default=0, db_column="legacy")),',
        )
        project.migration(
            "shop",
            "0003_a",
            'migrations.AlterField("item", "a", models.IntegerField(default=0)),',
        )
        project.migration(
            "shop",
            "0004_legacy",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        project.migration(
            "shop",
            "0005_a",
            'migrations.AlterField("item", "a", models.IntegerField(default=1)),',
        )
        result = project.check_equivalence()
        assert result.removals == []

    def test_same_column_still_superseded(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.CharField(max_length=20, db_column="n")),',
        )
        project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "name", models.CharField(max_length=30, db_column="n")),',
        )
        project.migration(
            "shop",
            "0004_c",
            'migrations.AlterField("item", "name", models.CharField(max_length=40, db_column="n")),',
        )
        result = project.check_equivalence()
        assert [r.kind for r in result.removals] == ["superseded"]


class TestElidableScope:
    def test_unordered_elidable_blocks(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration("shop", "0002_coupon", COUPON)
        project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        project.migration(
            "other",
            "0001_initial",
            "migrations.RunSQL(\"SELECT 'coupon'\", elidable=True),",
            deps=[("shop", "0001_initial")],
        )
        result = project.check_equivalence("shop")
        assert result.removals == []
        assert blocked(result)["shop.Coupon"] == (
            "RunSQL in other.0001_initial mentions 'coupon'",
        )
        assert "RunSQL" in project.read("other", "0001_initial")

    def test_ordered_elidable_reported(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_coupon",
            COUPON + "migrations.RunSQL(\"SELECT 'coupon'\", elidable=True),",
        )
        project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        result = project.check_equivalence()
        assert [r.description for r in result.removals if r.kind == "elidable"] == [
            "elidable RunSQL in 0002_coupon (mentions 'coupon', removed with shop.Coupon)"
        ]

    def test_elidable_in_excluded_app_blocks(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration("shop", "0002_coupon", COUPON)
        project.migration(
            "other",
            "0001_initial",
            "migrations.RunSQL(\"SELECT 'coupon'\", elidable=True),",
            deps=[("shop", "0002_coupon")],
        )
        project.migration(
            "shop",
            "0003_drop",
            'migrations.DeleteModel("Coupon"),',
            extra_deps=[("other", "0001_initial")],
        )
        result = project.compact("shop", exclude=["other"])
        assert blocked(result)["shop.Coupon"] == (
            "elidable RunSQL in other.0001_initial mentions 'coupon' but cannot be"
            " removed: app 'other' is excluded",
        )

    def test_elidable_flag_respects_exclude(self, project):
        project.migration(
            "shop",
            "0001_initial",
            ITEM + "migrations.RunSQL('SELECT 1', elidable=True),",
        )
        project.migration(
            "other", "0001_initial", "migrations.RunSQL('SELECT 2', elidable=True),"
        )
        result = project.compact(elidable=True, exclude=["other"])
        assert kinds(result) == [("elidable", "shop")]
        assert (
            result.removals[0].description
            == "elidable RunSQL in 0001_initial (--elidable)"
        )


def test_excluded_child_blocks_model(project):
    project.migration("shop", "0001_initial", ITEM + COUPON)
    project.migration("shop", "0002_drop", 'migrations.DeleteModel("Coupon"),')
    result = project.compact(exclude=["shop.Coupon.code"])
    assert result.removals == []
    assert blocked(result)["shop.Coupon"] == (
        "it contains the excluded field shop.Coupon.code",
    )


COUPON = """
    migrations.CreateModel(
        name="Coupon",
        fields=[
            ("id", models.AutoField(primary_key=True)),
            ("code", models.CharField(max_length=5)),
        ],
    ),
"""


# Data migrations and what removals change for their rows (safety review)


def test_superseded_alter_field_after_seeded_rows(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_seed",
        "migrations.RunPython(seed),",
        extra="""
        def seed(apps, schema_editor):
            alias = schema_editor.connection.alias
            apps.get_model("shop", "Item").objects.using(alias).create(name="a")
        """,
    )
    project.migration(
        "shop",
        "0003_level",
        """
        migrations.AddField("item", "level", models.IntegerField(null=True)),
        migrations.AlterField("item", "level", models.IntegerField(default=1)),
        migrations.AlterField("item", "level", models.IntegerField(null=True, default=2)),
        migrations.AlterField("item", "level", models.IntegerField(null=True, default=3)),
        """,
    )
    # The first AlterField fills the seeded row's NULL with 1: kept. The
    # second only changes the default: superseded.
    result = project.compact(skip_kinds=["fold"])
    assert kinds(result) == [("superseded", "shop.Item.level")]
    assert result.removed_positions == {(("shop", "0003_level"), 2)}


UNIQUE_SEED = """
def seed(apps, schema_editor):
    alias = schema_editor.connection.alias
    Item = apps.get_model("shop", "Item")
    Item.objects.using(alias).bulk_create(
        [Item(name="a"), Item(name="a")], ignore_conflicts=True
    )
"""


@pytest.mark.parametrize(
    "add, remove",
    [
        (
            'migrations.AddConstraint("item", models.UniqueConstraint('
            'fields=["name"], name="uniq_name")),',
            'migrations.RemoveConstraint("item", "uniq_name"),',
        ),
        (
            'migrations.AlterUniqueTogether("item", {("name", "id")}),',
            'migrations.AlterUniqueTogether("item", set()),',
        ),
    ],
)
def test_enforced_constraint_kept_for_data_migrations(project, add, remove):
    project.migration("shop", "0001_initial", ITEM + add)
    project.migration(
        "shop", "0002_seed", "migrations.RunPython(seed),", extra=UNIQUE_SEED
    )
    project.migration("shop", "0003_drop", remove)
    result = project.compact(skip_kinds=["fold"])
    assert kinds(result) == []
    (reasons,) = blocked(result).values()
    assert reasons == ("RunPython in shop.0002_seed mentions 'item'",)


def test_enforced_constraint_trial_unlocks(project):
    project.migration(
        "shop",
        "0001_initial",
        ITEM + 'migrations.AddConstraint("item", models.UniqueConstraint('
        'fields=["name"], name="uniq_name")),',
    )
    project.migration(
        "shop",
        "0002_count",
        "migrations.RunPython(count),",
        extra="""
        def count(apps, schema_editor):
            alias = schema_editor.connection.alias
            apps.get_model("shop", "Item").objects.using(alias).count()
        """,
    )
    project.migration(
        "shop", "0003_drop", 'migrations.RemoveConstraint("item", "uniq_name"),'
    )
    assert kinds(project.compact(skip_kinds=["fold"])) == []
    assert kinds(project.compact(static=False, skip_kinds=["fold"])) == [
        ("constraint", "shop.Item")
    ]


def test_permissions_for_every_model_block(project):
    project.migration(
        "shop",
        "0001_initial",
        ITEM
        + 'migrations.CreateModel("Coupon", [("id", models.AutoField(primary_key=True))]),',
    )
    project.migration(
        "shop",
        "0002_permissions",
        "migrations.RunPython(create_all, elidable=True),",
        extra="""
        def create_all(apps, schema_editor):
            from django.contrib.auth.management import create_permissions

            for app_config in apps.get_app_configs():
                create_permissions(app_config, apps=apps, verbosity=0)
        """,
    )
    project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
    result = project.compact()
    assert kinds(result) == []
    assert blocked(result)["shop.Coupon"] == (
        "RunPython in shop.0002_permissions acts on every model (create_permissions)",
    )


def test_superseded_alter_field_window_mentioned(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_level",
        """
        migrations.AddField("item", "level", models.IntegerField(null=True)),
        migrations.AlterField("item", "level", models.IntegerField(null=True, default=1)),
        migrations.RunSQL("SELECT level FROM shop_item"),
        migrations.AlterField("item", "level", models.IntegerField(null=True, default=2)),
        """,
    )
    assert kinds(project.compact(skip_kinds=["fold"])) == []


def test_enforced_constraint_elidable_not_removed_alongside(project):
    # An elidable operation naming the constraint's model blocks its removal
    # (the scan looks for the model too), but isn't removed with it.
    project.migration(
        "shop",
        "0001_initial",
        ITEM + 'migrations.AddConstraint("item", models.UniqueConstraint('
        'fields=["name"], name="uniq_name")),',
    )
    project.migration(
        "shop",
        "0002_view",
        'migrations.RunSQL("DROP VIEW IF EXISTS shop_item_names", elidable=True),',
    )
    project.migration(
        "shop", "0003_drop", 'migrations.RemoveConstraint("item", "uniq_name"),'
    )
    result = project.compact(skip_kinds=["fold"])
    assert kinds(result) == []
    assert blocked(result)["shop.Item constraint 'uniq_name'"] == (
        "RunSQL in shop.0002_view mentions 'item'",
    )
