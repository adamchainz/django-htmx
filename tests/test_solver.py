"""What keeps removals and superseded operations from going."""

from __future__ import annotations

import pytest
from unittest_parametrize import parametrize

from django_compact_migrations import history
from django_compact_migrations.core import CompactError
from tests.project import (
    ITEM,
    NO_FOLD,
    Project,
    ProjectTestCase,
    blocked,
    kinds,
    only,
)

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


# Superseded AlterFields


# Superseded model options


TAG = """
    migrations.CreateModel(
        name="Tag",
        fields=[
            ("id", models.AutoField(primary_key=True)),
            ("code", models.CharField(max_length=5, unique=True)),
        ],
    ),
"""


class SupersededReleasesTests(ProjectTestCase):
    def test_to_field_released(self):
        self.project.migration("shop", "0001_initial", ITEM + TAG)
        self.project.migration(
            "shop",
            "0003_tag",
            'migrations.AddField("item", "tag", models.ForeignKey("shop.tag",'
            ' models.CASCADE, to_field="code", null=True)),',
        )
        self.project.migration(
            "shop",
            "0004_alter",
            'migrations.AlterField("item", "tag", models.ForeignKey("shop.tag",'
            " models.CASCADE, null=True)),",
        )
        self.project.migration(
            "shop", "0005_drop_code", 'migrations.RemoveField("tag", "code"),'
        )
        self.project.migration(
            "shop",
            "0006_blank",
            'migrations.AlterField("item", "tag", models.ForeignKey("shop.tag",'
            " models.CASCADE, null=True, blank=True)),",
        )
        result = self.project.check_equivalence()
        assert result.removals == []

    def test_db_column_released(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AddField("item", "a", models.IntegerField(default=0, db_column="legacy")),',
        )
        self.project.migration(
            "shop",
            "0003_a",
            'migrations.AlterField("item", "a", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop",
            "0004_legacy",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop",
            "0005_a",
            'migrations.AlterField("item", "a", models.IntegerField(default=1)),',
        )
        result = self.project.check_equivalence()
        assert result.removals == []

    def test_same_column_still_superseded(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.CharField(max_length=20, db_column="n")),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "name", models.CharField(max_length=30, db_column="n")),',
        )
        self.project.migration(
            "shop",
            "0004_c",
            'migrations.AlterField("item", "name", models.CharField(max_length=40, db_column="n")),',
        )
        result = self.project.check_equivalence()
        assert [r.kind for r in result.removals] == ["superseded"]


class ElidableScopeTests(ProjectTestCase):
    def test_unordered_elidable_blocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_coupon", COUPON)
        self.project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        self.project.migration(
            "other",
            "0001_initial",
            "migrations.RunSQL(\"SELECT 'coupon'\", elidable=True),",
            deps=[("shop", "0001_initial")],
        )
        result = self.project.check_equivalence("shop")
        assert result.removals == []
        assert blocked(result)["shop.Coupon"] == (
            "RunSQL in other.0001_initial mentions 'coupon'",
        )
        assert "RunSQL" in self.project.read("other", "0001_initial")

    def test_ordered_elidable_reported(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_coupon",
            COUPON + "migrations.RunSQL(\"SELECT 'coupon'\", elidable=True),",
        )
        self.project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        result = self.project.check_equivalence()
        assert [r.description for r in result.removals if r.kind == "elidable"] == [
            "elidable RunSQL in 0002_coupon (mentions 'coupon', removed with shop.Coupon)"
        ]

    def test_elidable_in_excluded_app_blocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_coupon", COUPON)
        self.project.migration(
            "other",
            "0001_initial",
            "migrations.RunSQL(\"SELECT 'coupon'\", elidable=True),",
            deps=[("shop", "0002_coupon")],
        )
        self.project.migration(
            "shop",
            "0003_drop",
            'migrations.DeleteModel("Coupon"),',
            extra_deps=[("other", "0001_initial")],
        )
        result = self.project.compact("shop", exclude=["other"])
        assert blocked(result)["shop.Coupon"] == (
            "elidable RunSQL in other.0001_initial mentions 'coupon' but cannot be"
            " removed: app 'other' is excluded",
        )

    def test_elidable_flag_respects_exclude(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM + "migrations.RunSQL('SELECT 1', elidable=True),",
        )
        self.project.migration(
            "other", "0001_initial", "migrations.RunSQL('SELECT 2', elidable=True),"
        )
        result = self.project.compact(elidable=True, exclude=["other"])
        assert kinds(result) == [("elidable", "shop")]
        assert (
            result.removals[0].description
            == "elidable RunSQL in 0001_initial (--elidable)"
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


UNIQUE_SEED = """
def seed(apps, schema_editor):
    alias = schema_editor.connection.alias
    Item = apps.get_model("shop", "Item")
    Item.objects.using(alias).bulk_create(
        [Item(name="a"), Item(name="a")], ignore_conflicts=True
    )
"""


class SolverTests(ProjectTestCase):
    def test_unrequired_requirement_restored(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_add", ADD + "\n" + INDEX)
        self.project.migration(
            "shop",
            "0003_remove",
            """
            migrations.RunSQL("SELECT legacy FROM shop_item"),
            migrations.RemoveIndex("item", "l_idx"),
            migrations.RemoveField("item", "legacy"),
            """,
        )
        # The field can't go, so neither goes the index it required.
        result = self.project.compact(**only("field"))
        assert result.removals == []
        assert list(blocked(result)) == ["shop.Item.legacy"]

    def test_elidable_hit_dropped_once(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            ADD
            + '\nmigrations.RunSQL("UPDATE shop_item SET legacy = 1", elidable=True),',
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.compact(elidable=True)
        assert ("field", "shop.Item.legacy") in kinds(result)
        assert [k for k, _ in kinds(result)].count("elidable") == 1

    def test_elidable_in_excluded_app(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration("shop", "0002_add", ADD)
        self.project.migration(
            "other",
            "0001_initial",
            'migrations.RunSQL("UPDATE shop_item SET legacy = 1", elidable=True),',
            deps=[("shop", "0002_add")],
        )
        self.project.migration(
            "shop",
            "0003_remove",
            'migrations.RemoveField("item", "legacy"),',
            extra_deps=[("other", "0001_initial")],
        )
        result = self.project.compact(exclude=["other"])
        assert blocked(result)["shop.Item.legacy"] == (
            "elidable RunSQL in other.0001_initial mentions 'legacy' but cannot be"
            " removed: app 'other' is excluded",
        )

    def test_poisoned_together_blocks_model(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
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
        (reason,) = blocked(self.project.compact())["shop.Coupon"]
        assert reason.startswith(
            "its together shop.Coupon unique_together (id, code) was"
        )

    def test_alter_field_in_unparsable_migration(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            source=unparsable(
                'migrations.AlterField("item", "name", models.CharField(max_length=20))',
                "0001_initial",
            ),
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "name", models.CharField(max_length=30)),',
        )
        assert superseded(self.project.compact()) == []

    @parametrize(
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
    def test_unordered(self, first, second):
        self.project.migration("shop", "0001_initial", ITEM)
        # Django runs the second branch first.
        self.project.migration("shop", "0002_b", first, deps=[("shop", "0001_initial")])
        self.project.migration(
            "shop", "0002_a", second, deps=[("shop", "0001_initial")]
        )
        self.project.migration(
            "shop", "0003_merge", deps=[("shop", "0002_a"), ("shop", "0002_b")]
        )
        assert superseded(self.project.compact()) == []

    def test_many_to_many_alters_kept(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM
            + 'migrations.AddField("item", "links", models.ManyToManyField("shop.item")),',
        )
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "links", models.ManyToManyField("shop.item", blank=True)),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "links", models.ManyToManyField("shop.item")),',
        )
        assert superseded(self.project.compact()) == []

    def test_internal_type_differs(self):
        self.project.migration("shop", "0001_initial", ITEM)
        (self.project.path("shop").parent / "fields.py").write_text(
            "from django.db import models\n\n\n"
            "class Flexible(models.Field):\n"
            "    def __init__(self, *args, text=False, **kwargs):\n"
            "        self.text = text\n"
            "        super().__init__(*args, **kwargs)\n\n"
            "    def get_internal_type(self):\n"
            "        return 'TextField' if self.text else 'IntegerField'\n"
        )
        extra = f"from {self.project.package}.shop.fields import Flexible"
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "f", Flexible(text=True, null=True)),',
            extra=extra,
        )
        self.project.migration(
            "shop",
            "0003_a",
            'migrations.AlterField("item", "f", Flexible(null=True)),',
            extra=extra,
        )
        self.project.migration(
            "shop",
            "0004_b",
            'migrations.AlterField("item", "f", Flexible(null=True, blank=True)),',
            extra=extra,
        )
        # The second change makes the same change of type from the start.
        assert superseded(self.project.compact()) == ["shop.Item.f"]

    def test_options_outside_alter_keys(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterModelOptions("item", {"verbose_name": "a", "db_table_comment": "c"}),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterModelOptions("item", {"verbose_name": "b"}),',
        )
        assert superseded(self.project.compact()) == []

    def test_options_in_unparsable_migration(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            source=unparsable(
                'migrations.AlterModelOptions("item", {"verbose_name": "a"})',
                "0001_initial",
            ),
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterModelOptions("item", {"verbose_name": "b"}),',
        )
        assert superseded(self.project.compact()) == []

    def test_index_on_two_dead_fields(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "a", models.IntegerField(default=0)),
            migrations.AddField("item", "b", models.IntegerField(default=0)),
            migrations.AddIndex("item", models.Index(fields=["a", "b"], name="ab_idx")),
            migrations.RunSQL("UPDATE shop_item SET a = 1, b = 2", elidable=True),
            """,
        )
        self.project.migration(
            "shop",
            "0003_remove",
            """
            migrations.RemoveIndex("item", "ab_idx"),
            migrations.RemoveField("item", "a"),
            migrations.RemoveField("item", "b"),
            """,
        )
        result = self.project.check_equivalence()
        assert [k for k, _ in kinds(result)] == ["elidable", "field", "field", "index"]

    def test_superseded_with_reference_before(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM
            + 'migrations.AddIndex("item", models.Index(fields=["name"], name="n_idx")),',
        )
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "name", models.CharField(max_length=30)),',
        )
        assert superseded(self.project.check_equivalence()) == ["shop.Item.name"]

    def test_excluded_child_blocks_model(self):
        self.project.migration("shop", "0001_initial", ITEM + COUPON)
        self.project.migration("shop", "0002_drop", 'migrations.DeleteModel("Coupon"),')
        result = self.project.compact(exclude=["shop.Coupon.code"])
        assert result.removals == []
        assert blocked(result)["shop.Coupon"] == (
            "it contains the excluded field shop.Coupon.code",
        )

    def test_superseded_alter_field_after_seeded_rows(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_seed",
            "migrations.RunPython(seed),",
            extra="""
            def seed(apps, schema_editor):
                alias = schema_editor.connection.alias
                apps.get_model("shop", "Item").objects.using(alias).create(name="a")
            """,
        )
        self.project.migration(
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
        result = self.project.compact(skip_kinds=["fold"])
        assert kinds(result) == [("superseded", "shop.Item.level")]
        assert result.removed_positions == {(("shop", "0003_level"), 2)}

    @parametrize(
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
    def test_enforced_constraint_kept_for_data_migrations(self, add, remove):
        self.project.migration("shop", "0001_initial", ITEM + add)
        self.project.migration(
            "shop", "0002_seed", "migrations.RunPython(seed),", extra=UNIQUE_SEED
        )
        self.project.migration("shop", "0003_drop", remove)
        result = self.project.compact(skip_kinds=["fold"])
        assert kinds(result) == []
        (reasons,) = blocked(result).values()
        assert reasons == ("RunPython in shop.0002_seed mentions 'item'",)

    def test_enforced_constraint_trial_unlocks(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM + 'migrations.AddConstraint("item", models.UniqueConstraint('
            'fields=["name"], name="uniq_name")),',
        )
        self.project.migration(
            "shop",
            "0002_count",
            "migrations.RunPython(count),",
            extra="""
            def count(apps, schema_editor):
                alias = schema_editor.connection.alias
                apps.get_model("shop", "Item").objects.using(alias).count()
            """,
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.RemoveConstraint("item", "uniq_name"),'
        )
        assert kinds(self.project.compact(skip_kinds=["fold"])) == []
        assert kinds(self.project.compact(static=False, skip_kinds=["fold"])) == [
            ("constraint", "shop.Item")
        ]

    def test_permissions_for_every_model_block(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM
            + 'migrations.CreateModel("Coupon", [("id", models.AutoField(primary_key=True))]),',
        )
        self.project.migration(
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
        self.project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        result = self.project.compact()
        assert kinds(result) == []
        assert blocked(result)["shop.Coupon"] == (
            "RunPython in shop.0002_permissions acts on every model (create_permissions)",
        )

    def test_content_types_for_every_model_blocks(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM
            + 'migrations.CreateModel("Coupon", [("id", models.AutoField(primary_key=True))]),',
        )
        self.project.migration(
            "shop",
            "0002_types",
            "migrations.RunPython(create_all),",
            extra="""
            def create_all(apps, schema_editor):
                ContentType = apps.get_model("contenttypes", "ContentType")
                for model in apps.get_models():
                    ContentType.objects.get_for_model(model)
            """,
        )
        self.project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        # The row for the model stays behind, whatever its id.
        result = self.project.compact()
        assert kinds(result) == []
        assert blocked(result)["shop.Coupon"] == (
            "RunPython in shop.0002_types acts on every model (get_for_model)",
        )

    def test_superseded_alter_field_window_mentioned(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_level",
            """
            migrations.AddField("item", "level", models.IntegerField(null=True)),
            migrations.AlterField("item", "level", models.IntegerField(null=True, default=1)),
            migrations.RunSQL("SELECT level FROM shop_item"),
            migrations.AlterField("item", "level", models.IntegerField(null=True, default=2)),
            """,
        )
        assert kinds(self.project.compact(skip_kinds=["fold"])) == []

    def test_enforced_constraint_elidable_not_removed_alongside(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM + 'migrations.AddConstraint("item", models.UniqueConstraint('
            'fields=["name"], name="uniq_name")),',
        )
        self.project.migration(
            "shop",
            "0002_view",
            'migrations.RunSQL("DROP VIEW IF EXISTS shop_item_names", elidable=True),',
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.RemoveConstraint("item", "uniq_name"),'
        )
        result = self.project.compact(skip_kinds=["fold"])
        assert kinds(result) == []
        assert blocked(result)["shop.Item constraint 'uniq_name'"] == (
            "RunSQL in shop.0002_view mentions 'item'",
        )


class ModelTests(ProjectTestCase):
    def test_removed(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_coupon",
            """
            migrations.CreateModel(
                name="Coupon",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("code", models.CharField(max_length=10, unique=True)),
                ],
                options={"ordering": ["code"]},
            ),
            migrations.AddField("coupon", "value", models.IntegerField(default=1)),
            migrations.AlterField("coupon", "value", models.IntegerField(default=2)),
            migrations.AddIndex("coupon", models.Index(fields=["value"], name="value_idx")),
            """,
        )
        self.project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        result = self.project.check_equivalence()
        assert kinds(result) == [("model", "shop.Coupon")]
        removal = result.removals[0]
        assert (
            removal.description == "model created in 0002_coupon, deleted in 0003_drop"
        )
        assert [e.operation for e in removal.elements] == [
            "CreateModel",
            "AddField",
            "AlterField",
            "AddIndex",
            "DeleteModel",
        ]
        assert result.removed_operations == 5
        assert result.empty_migrations == [
            ("shop", "0002_coupon"),
            ("shop", "0003_drop"),
        ]
        assert "operations = []" in self.project.read("shop", "0003_drop")

    def test_renamed_model(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_old",
            """
            migrations.CreateModel(
                name="Old",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.RenameModel("Old", "New"),
            """,
        )
        self.project.migration("shop", "0003_drop", 'migrations.DeleteModel("New"),')
        result = self.project.check_equivalence()
        assert kinds(result) == [("model", "shop.New")]
        assert len(result.removals[0].elements) == 3

    def test_name_reuse(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Thing",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            """,
        )
        self.project.migration("shop", "0002_drop", 'migrations.DeleteModel("Thing"),')
        self.project.migration(
            "shop",
            "0003_again",
            """
            migrations.CreateModel(
                name="Thing",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("size", models.IntegerField()),
                ],
            ),
            """,
        )
        result = self.project.check_equivalence()
        assert [r.description for r in result.removals] == [
            "model created in 0001_initial, deleted in 0002_drop"
        ]
        assert "size" in self.project.read("shop", "0003_again")

    def test_live_model_untouched(self):
        self.project.migration("shop", "0001_initial", ITEM)
        result = self.project.compact()
        assert result.removals == []
        assert not result.changed


class FieldTests(ProjectTestCase):
    def test_add_remove(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.AlterField("item", "legacy", models.IntegerField(null=True)),
            migrations.RenameField("item", "legacy", "old"),
            """,
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "old"),'
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.old")]
        assert result.removals[0].description == (
            "field added in 0002_add, removed in 0003_remove"
        )
        assert result.removed_operations == 4

    def test_create_model_entry(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    # Going away soon.
                    ("legacy", models.IntegerField()),
                    ("name", models.CharField(max_length=10)),
                ],
            ),
            """,
        )
        self.project.migration(
            "shop", "0002_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.legacy")]
        assert result.removed_operations == 1
        assert result.edited_operations == 1
        assert self.project.read("shop", "0001_initial").endswith(
            """\
        migrations.CreateModel(
            name="Item",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("name", models.CharField(max_length=10)),
            ],
        ),
    ]
"""
        )

    def test_primary_key_blocked(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[("code", models.CharField(max_length=5, primary_key=True))],
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_swap",
            """
            migrations.AddField("item", "id", models.IntegerField(default=0)),
            migrations.RemoveField("item", "code"),
            """,
        )
        result = self.project.compact(**NO_FOLD)
        assert result.removals == []
        assert blocked(result) == {"shop.Item.code": ("it is or was a primary key",)}

    def test_referenced_by_live_index(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.AddIndex("item", models.Index(fields=["legacy"], name="legacy_idx")),
            """,
        )
        # A RemoveField that leaves a stale index in state (inconsistent but
        # accepted by Django's state) must not be removed.
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.compact(**NO_FOLD)
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "referenced by AddIndex in shop.0002_add",
        )

    def test_generated_field_reference(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "price", models.IntegerField(default=0)),
            migrations.AddField(
                "item",
                "double",
                models.GeneratedField(
                    expression=models.F("price") * 2,
                    output_field=models.IntegerField(),
                    db_persist=True,
                ),
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0003_remove",
            """
            migrations.RemoveField("item", "double"),
            migrations.RemoveField("item", "price"),
            """,
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [
            ("field", "shop.Item.double"),
            ("field", "shop.Item.price"),
        ]

    def test_generated_field_blocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "price", models.IntegerField(default=0)),
            migrations.AddField(
                "item",
                "double",
                models.GeneratedField(
                    expression=models.F("price") * 2,
                    output_field=models.IntegerField(),
                    db_persist=True,
                ),
            ),
            """,
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "price"),'
        )
        result = self.project.compact("shop.Item.price")
        assert result.removals == []
        assert blocked(result)["shop.Item.price"] == (
            "referenced by AddField in shop.0002_add",
        )


class IndexAndConstraintTests(ProjectTestCase):
    def test_index(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_index",
            """
            migrations.AddIndex("item", models.Index(fields=["name"], name="name_idx")),
            migrations.RenameIndex("item", new_name="name_idx2", old_name="name_idx"),
            """,
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.RemoveIndex("item", "name_idx2"),'
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("index", "shop.Item")]
        assert result.removals[0].description == (
            "index 'name_idx' added in 0002_index, removed in 0003_drop"
        )
        assert result.removed_operations == 3

    def test_create_model_index_entry(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("name", models.CharField(max_length=10)),
                ],
                options={
                    "indexes": [models.Index(fields=["name"], name="a_idx"), models.Index(fields=["id", "name"], name="b_idx")],
                },
            ),
            """,
        )
        self.project.migration(
            "shop", "0002_drop", 'migrations.RemoveIndex("item", "a_idx"),'
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("index", "shop.Item")]
        assert (
            '"indexes": [models.Index(fields=["id", "name"], name="b_idx")],'
            in self.project.read("shop", "0001_initial")
        )

    def test_constraint(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_constraint",
            """
            migrations.AddConstraint(
                "item",
                models.CheckConstraint(condition=models.Q(name__gt=""), name="name_set"),
            ),
            migrations.AlterConstraint(
                "item",
                "name_set",
                models.CheckConstraint(
                    condition=models.Q(name__gt=""),
                    name="name_set",
                    violation_error_message="Name!",
                ),
            ),
            """,
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.RemoveConstraint("item", "name_set"),'
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("constraint", "shop.Item")]
        assert result.removed_operations == 3

    def test_create_model_constraint_entry(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("qty", models.IntegerField()),
                ],
                options={
                    "constraints": [
                        models.CheckConstraint(
                            condition=models.Q(qty__gte=0), name="qty_positive"
                        ),
                    ],
                },
            ),
            """,
        )
        self.project.migration(
            "shop", "0002_drop", 'migrations.RemoveConstraint("item", "qty_positive"),'
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("constraint", "shop.Item")]
        assert "constraints" not in self.project.read("shop", "0001_initial")
        assert "options={}," in self.project.read("shop", "0001_initial")

    def test_constraint_keeps_field_alive(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_qty",
            """
            migrations.AddField("item", "qty", models.IntegerField(default=0)),
            migrations.AddConstraint(
                "item",
                models.CheckConstraint(condition=models.Q(qty__gte=0), name="qty_positive"),
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0003_drop",
            """
            migrations.RemoveConstraint("item", "qty_positive"),
            migrations.RemoveField("item", "qty"),
            """,
        )
        # Selecting only the field pulls in the constraint that references it.
        result = self.project.check_equivalence("shop.Item.qty")
        assert sorted(kinds(result)) == [
            ("constraint", "shop.Item"),
            ("field", "shop.Item.qty"),
        ]

    def test_kind_selection_pulls_in(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_qty",
            """
            migrations.AddField("item", "qty", models.IntegerField(default=0)),
            migrations.AddConstraint(
                "item",
                models.CheckConstraint(condition=models.Q(qty__gte=0), name="qty_positive"),
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0003_drop",
            """
            migrations.RemoveConstraint("item", "qty_positive"),
            migrations.RemoveField("item", "qty"),
            """,
        )
        # The constraint is not a selected kind but is required by the field.
        result = self.project.compact("shop.Item.qty", **only("field"))
        assert sorted(kinds(result)) == [
            ("constraint", "shop.Item"),
            ("field", "shop.Item.qty"),
        ]
        # Only constraints selected: the field stays, the constraint goes.
        result = self.project.compact(**only("constraint"))
        assert kinds(result) == [("constraint", "shop.Item")]


class TogetherTests(ProjectTestCase):
    def test_alter_together_pair(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_unique",
            'migrations.AlterUniqueTogether("item", {("id", "name")}),',
        )
        self.project.migration(
            "shop", "0003_not_unique", 'migrations.AlterUniqueTogether("item", set()),'
        )
        result = self.project.check_equivalence()
        assert sorted(kinds(result)) == [
            ("together", "shop.Item"),
            ("together", "shop.Item"),
            ("together", "shop.Item"),
        ]
        assert result.removed_operations == 2
        descriptions = sorted(r.description for r in result.removals)
        assert descriptions[0] == (
            "AlterUniqueTogether in 0002_unique no longer changes anything"
        )
        assert descriptions[-1] == (
            "unique_together (id, name) added in 0002_unique, removed in 0003_not_unique"
        )

    def test_entry_stripped(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_unique",
            'migrations.AlterUniqueTogether("item", {("id", "name"), ("name", "id")}),',
        )
        self.project.migration(
            "shop",
            "0003_less",
            'migrations.AlterUniqueTogether("item", {("name", "id")}),',
        )
        result = self.project.check_equivalence(**NO_FOLD)
        assert '{("name", "id")}' in self.project.read("shop", "0002_unique")
        assert result.removed_operations == 1
        assert result.edited_operations == 1

    def test_create_model_option_and_rename_field(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("a", models.IntegerField()),
                    ("b", models.IntegerField()),
                ],
                options={
                    "unique_together": {("a", "b")},
                },
            ),
            """,
        )
        self.project.migration(
            "shop", "0002_rename", 'migrations.RenameField("item", "a", "c"),'
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.AlterUniqueTogether("item", set()),'
        )
        result = self.project.check_equivalence()
        assert result.removed_operations == 1
        assert "options" not in self.project.read("shop", "0001_initial")

    def test_rename_index_old_fields_chain(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("a", models.IntegerField()),
                    ("b", models.IntegerField()),
                ],
                options={"index_together": {("a", "b")}},
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_rename",
            'migrations.RenameIndex("item", new_name="a_b_idx", old_fields=("a", "b")),',
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.RemoveIndex("item", "a_b_idx"),'
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("together", "shop.Item")]
        assert result.removed_operations == 2
        assert result.edited_operations == 1
        assert "index_together" not in self.project.read("shop", "0001_initial")

    def test_together_keeps_field(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "code", models.IntegerField(default=0)),
            migrations.AlterUniqueTogether("item", {("name", "code")}),
            """,
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "code"),'
        )
        result = self.project.compact(**NO_FOLD)
        assert result.removals == []
        assert blocked(result)["shop.Item.code"][0].startswith(
            "referenced by AlterUniqueTogether"
        )


def author_book(project: Project, app: str = "library") -> None:
    project.migration(
        app,
        "0001_initial",
        """
        migrations.CreateModel(
            name="Author",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
        """,
    )


class RelationsTests(ProjectTestCase):
    def test_cross_app_fk_pulled_in(self):
        author_book(self.project)
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Book",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("author", models.ForeignKey("library.author", models.CASCADE)),
                ],
            ),
            """,
            deps=[("library", "0001_initial")],
        )
        self.project.migration(
            "shop", "0002_drop_author", 'migrations.RemoveField("book", "author"),'
        )
        self.project.migration(
            "library",
            "0002_drop",
            'migrations.DeleteModel("Author"),',
            extra_deps=[("shop", "0002_drop_author")],
        )
        result = self.project.check_equivalence("library")
        assert kinds(result) == [
            ("field", "shop.Book.author"),
            ("model", "library.Author"),
        ]

    def test_cross_app_fk_excluded(self):
        author_book(self.project)
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Book",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("author", models.ForeignKey("library.author", models.CASCADE)),
                ],
            ),
            """,
            deps=[("library", "0001_initial")],
        )
        self.project.migration(
            "shop", "0002_drop_author", 'migrations.RemoveField("book", "author"),'
        )
        self.project.migration(
            "library",
            "0002_drop",
            'migrations.DeleteModel("Author"),',
            extra_deps=[("shop", "0002_drop_author")],
        )
        result = self.project.compact("library", exclude=["shop"])
        assert result.removals == []
        assert blocked(result) == {
            "library.Author": (
                "referenced by CreateModel in shop.0001_initial (field 'author')",
            )
        }

    def test_live_fk_blocks(self):
        author_book(self.project)
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Book",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.AddField("book", "author", models.ForeignKey("library.author", models.SET_NULL, null=True)),
            """,
            deps=[("library", "0001_initial")],
        )
        self.project.migration(
            "library",
            "0002_drop",
            # Inconsistent but accepted by the state: a dangling FK remains.
            'migrations.DeleteModel("Author"),',
            extra_deps=[("shop", "0001_initial")],
        )
        result = self.project.compact(**NO_FOLD)
        assert result.removals == []
        assert blocked(result)["library.Author"] == (
            "referenced by AddField in shop.0001_initial",
        )

    def test_to_field(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("code", models.CharField(max_length=5, unique=True)),
                ],
            ),
            migrations.CreateModel(
                name="Line",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("item", models.ForeignKey("shop.item", models.CASCADE, to_field="code")),
                ],
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_drop",
            """
            migrations.AlterField("line", "item", models.ForeignKey("shop.item", models.CASCADE)),
            migrations.RemoveField("item", "code"),
            """,
        )
        result = self.project.compact("shop.Item.code")
        assert result.removals == []
        assert blocked(result)["shop.Item.code"] == (
            "referenced by CreateModel in shop.0001_initial (field 'item')",
        )

    def test_m2m_auto_through(self):
        author_book(self.project)
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Book",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("authors", models.ManyToManyField("library.author")),
                ],
            ),
            """,
            deps=[("library", "0001_initial")],
        )
        self.project.migration(
            "shop", "0002_drop", 'migrations.RemoveField("book", "authors"),'
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("field", "shop.Book.authors")]

    def test_m2m_through(self):
        author_book(self.project)
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Book",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.CreateModel(
                name="Credit",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("book", models.ForeignKey("shop.book", models.CASCADE)),
                    ("author", models.ForeignKey("library.author", models.CASCADE)),
                ],
            ),
            migrations.AddField(
                "book",
                "authors",
                models.ManyToManyField("library.author", through="shop.Credit"),
            ),
            """,
            deps=[("library", "0001_initial")],
        )
        self.project.migration(
            "shop",
            "0002_drop",
            """
            migrations.RemoveField("book", "authors"),
            migrations.DeleteModel("Credit"),
            """,
        )
        result = self.project.check_equivalence("shop.Credit")
        assert kinds(result) == [
            ("field", "shop.Book.authors"),
            ("model", "shop.Credit"),
        ]

    def test_m2m_through_blocks(self):
        author_book(self.project)
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Book",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.CreateModel(
                name="Credit",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("book", models.ForeignKey("shop.book", models.CASCADE)),
                    ("author", models.ForeignKey("library.author", models.CASCADE)),
                ],
            ),
            migrations.AddField(
                "book",
                "authors",
                models.ManyToManyField("library.author", through="shop.Credit"),
            ),
            """,
            deps=[("library", "0001_initial")],
        )
        self.project.migration(
            "shop",
            "0002_swap",
            """
            migrations.AlterField("book", "authors", models.ManyToManyField("library.author")),
            migrations.DeleteModel("Credit"),
            """,
        )
        result = self.project.compact()
        assert result.removals == []
        assert blocked(result)["shop.Credit"] == (
            "referenced by AddField in shop.0001_initial",
        )

    def test_base_model(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Base",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.CreateModel(
                name="Child",
                fields=[
                    ("base_ptr", models.OneToOneField(auto_created=True, on_delete=models.CASCADE, parent_link=True, primary_key=True, serialize=False, to="shop.base")),
                ],
                bases=("shop.base",),
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_drop",
            """
            migrations.DeleteModel("Child"),
            migrations.DeleteModel("Base"),
            """,
        )
        result = self.project.check_equivalence("shop.Base")
        assert kinds(result) == [("model", "shop.Base"), ("model", "shop.Child")]

    def test_base_model_blocks(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Base",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.CreateModel(
                name="Child",
                fields=[("id", models.AutoField(primary_key=True))],
                bases=("shop.base",),
            ),
            """,
        )
        self.project.migration("shop", "0002_drop", 'migrations.DeleteModel("Base"),')
        result = self.project.compact()
        assert blocked(result)["shop.Base"] == (
            "referenced by CreateModel in shop.0001_initial",
        )

    def test_self_referencing(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Node",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("parent", models.ForeignKey("self", models.CASCADE, null=True)),
                ],
            ),
            """,
        )
        self.project.migration("shop", "0002_drop", 'migrations.DeleteModel("Node"),')
        result = self.project.check_equivalence()
        assert kinds(result) == [("model", "shop.Node")]

    def test_later_model_with_same_name_referenced(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Line",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("item", models.ForeignKey("shop.item", models.CASCADE)),
                ],
            ),
            migrations.CreateModel(
                name="Item",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_drop",
            """
            migrations.RemoveField("line", "item"),
            migrations.DeleteModel("Item"),
            """,
        )
        result = self.project.compact("shop.Item", **only("model"))
        assert kinds(result) == [
            ("field", "shop.Line.item"),
            ("model", "shop.Item"),
        ]
        self.project.migration(
            "shop",
            "0002_drop",
            'migrations.DeleteModel("Item"),',
            deps=[("shop", "0001_initial")],
        )
        result = self.project.compact("shop.Item")
        assert blocked(result)["shop.Item"] == (
            "referenced by CreateModel in shop.0001_initial (field 'item')",
        )


class SupersededTests(ProjectTestCase):
    def test_alter_field_chain(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "name", models.CharField(max_length=30)),',
        )
        self.project.migration(
            "shop",
            "0004_c",
            'migrations.AlterField("item", "name", models.CharField(max_length=40, db_index=True)),',
        )
        result = self.project.check_equivalence(**NO_FOLD)
        assert [(r.kind, r.description) for r in result.removals] == [
            ("superseded", "AlterField in 0002_a superseded by AlterField in 0003_b"),
            ("superseded", "AlterField in 0003_b superseded by AlterField in 0004_c"),
        ]
        assert "max_length=40" in self.project.read("shop", "0004_c")

    def test_alter_field_type_guard(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.TextField()),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "name", models.IntegerField(null=True)),',
        )
        result = self.project.compact(**NO_FOLD)
        assert result.removals == []

    def test_alter_field_type_guard_one_side(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "name", models.TextField()),',
        )
        result = self.project.check_equivalence(**NO_FOLD)
        assert [r.kind for r in result.removals] == ["superseded"]

    def test_alter_field_before_remove(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.TextField()),',
        )
        self.project.migration(
            "shop",
            "0003_index",
            'migrations.AddIndex("item", models.Index(fields=["name"], name="n_idx")),',
        )
        self.project.migration(
            "shop", "0004_b", 'migrations.RemoveField("item", "name"),'
        )
        # The index keeps the (dead) field; the AlterField is still superseded
        # by RemoveField but the index refers to the field meanwhile.
        result = self.project.compact(**NO_FOLD)
        assert result.removals == []
        assert "shop.Item.name" in blocked(result)

    def test_alter_field_rename_between(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            """
            migrations.AlterField("item", "name", models.CharField(max_length=20)),
            migrations.RenameField("item", "name", "title"),
            migrations.AlterField("item", "title", models.CharField(max_length=30)),
            """,
        )
        assert self.project.compact(**NO_FOLD).removals == []

    def test_alter_model_options(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterModelOptions("item", {"verbose_name": "thing"}),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterModelOptions("item", {"verbose_name": "item"}),',
        )
        result = self.project.check_equivalence(**NO_FOLD)
        assert [r.description for r in result.removals] == [
            "AlterModelOptions in 0002_a superseded by AlterModelOptions in 0003_b"
        ]

    def test_alter_model_options_managed(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterModelOptions("item", {"managed": False}),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterModelOptions("item", {}),',
        )
        assert self.project.compact().removals == []

    def test_alter_model_managers(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterModelManagers("item", [("things", models.Manager())]),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterModelManagers("item", [("objects", models.Manager())]),',
        )
        result = self.project.check_equivalence()
        assert [r.kind for r in result.removals] == ["superseded"]

    def test_alter_model_table_never_superseded(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop", "0002_a", 'migrations.AlterModelTable("item", "old_item"),'
        )
        self.project.migration(
            "shop",
            "0003_thing",
            """
            migrations.CreateModel(
                name="Thing",
                fields=[("id", models.AutoField(primary_key=True))],
                options={"db_table": "shop_item"},
            ),
            """,
        )
        self.project.migration(
            "shop", "0004_b", 'migrations.AlterModelTable("item", "archive_item"),'
        )
        result = self.project.check_equivalence()
        assert result.removals == []

    def test_alter_model_table_comment(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop", "0002_a", 'migrations.AlterModelTableComment("item", "a"),'
        )
        self.project.migration(
            "shop", "0003_b", 'migrations.AlterModelTableComment("item", "b"),'
        )
        result = self.project.check_equivalence(**NO_FOLD)
        assert [r.kind for r in result.removals] == ["superseded"]

    def test_alter_model_table_with_element_between(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            """
            migrations.AlterModelTableComment("item", "a"),
            migrations.AddField("item", "qty", models.IntegerField(default=0)),
            migrations.AlterModelTableComment("item", "b"),
            """,
        )
        assert self.project.compact(**NO_FOLD).removals == []

    def test_kind_filter(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop", "0002_a", 'migrations.AlterModelTableComment("item", "a"),'
        )
        self.project.migration(
            "shop", "0003_b", 'migrations.AlterModelTableComment("item", "b"),'
        )
        assert self.project.compact(**only("model", "field")).removals == []
        assert len(self.project.compact(**only("superseded")).removals) == 1
        assert len(self.project.compact("shop.Item", **NO_FOLD).removals) == 1
        assert self.project.compact("shop.Item.name", **NO_FOLD).removals == []
        assert self.project.compact(exclude=["shop.item"], **NO_FOLD).removals == []


class OpaqueTests(ProjectTestCase):
    def _history(self, project: Project, code: str, where: str = "0002_add") -> None:
        project.migration("shop", "0001_initial", ITEM)
        ops = 'migrations.AddField("item", "legacy", models.IntegerField(default=0)),\n'
        project.migration(
            "shop",
            "0002_add",
            ops + (code if where == "0002_add" else ""),
            extra="""
            def forwards(apps, schema_editor):
                Item = apps.get_model("shop", "Item")
                Item.objects.using(schema_editor.connection.alias).update(legacy=1)
            """,
        )
        project.migration(
            "shop",
            "0003_remove",
            (code if where == "0003_remove" else "")
            + 'migrations.RemoveField("item", "legacy"),',
            extra="""
            def forwards(apps, schema_editor):
                Item = apps.get_model("shop", "Item")
                Item.objects.using(schema_editor.connection.alias).update(legacy=1)
            """,
        )

    def test_scan_hit(self):
        self._history(self.project, "migrations.RunPython(forwards),\n")
        result = self.project.check_equivalence(**NO_FOLD)
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "RunPython in shop.0002_add mentions 'legacy'",
        )

    def test_scan_miss(self):
        self._history(self.project, "migrations.RunSQL('SELECT 1'),\n")
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.RunSQL("UPDATE shop_item SET name = 'x'"),
            """,
            deps=[("shop", "0001_initial")],
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.legacy")]

    def test_window(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM + "migrations.RunSQL(\"SELECT 'legacy'\"),",
        )
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop",
            "0003_remove",
            """
            migrations.RemoveField("item", "legacy"),
            migrations.RunSQL("SELECT 'legacy'"),
            """,
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.legacy")]

    def test_external_function(self):
        self.project.migration("shop", "0001_initial", ITEM)
        (self.project.root / self.project.package / "helpers.py").write_text(
            "def touch(apps, schema_editor):\n    apps.get_model('shop', 'Item').objects.filter(legacy=1)\n"
        )
        self.project.migration(
            "shop",
            "0002_add",
            f"""
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.RunPython({self.project.package}.helpers.touch),
            """,
            extra=f"import {self.project.package}.helpers",
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.compact()
        assert blocked(result)["shop.Item.legacy"] == (
            "RunPython in shop.0002_add mentions 'legacy'",
        )

    def test_unknown_source_matches_everything(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.RunPython(print),
            """,
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.compact()
        assert "mentions 'legacy'" in blocked(result)["shop.Item.legacy"][0]

    def test_noop_and_partial(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.RunPython(functools.partial(migrations.RunPython.noop), migrations.RunPython.noop),
            """,
            extra="import functools",
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.legacy")]

    def test_soft_reference(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "rank", models.IntegerField(default=0)),
            migrations.AlterModelOptions("item", {"ordering": ["-rank"]}),
            migrations.RunPython(forwards, elidable=False),
            """,
            extra="""
            def forwards(apps, schema_editor):
                list(apps.get_model("shop", "Item").objects.all())
            """,
        )
        self.project.migration(
            "shop",
            "0003_remove",
            """
            migrations.AlterModelOptions("item", {}),
            migrations.RemoveField("item", "rank"),
            """,
        )
        result = self.project.compact("shop.Item.rank")
        assert result.removals == []
        assert "orders by it" in blocked(result)["shop.Item.rank"][0]
        # Without the data migration the soft reference doesn't block.
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "rank", models.IntegerField(default=0)),
            migrations.AlterModelOptions("item", {"ordering": ["-rank"]}),
            """,
            deps=[("shop", "0001_initial")],
        )
        result = self.project.check_equivalence("shop.Item.rank")
        assert kinds(result) == [("field", "shop.Item.rank")]


class ElidableTests(ProjectTestCase):
    def test_removed_with_entity(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_coupon",
            """
            migrations.CreateModel(
                name="Coupon",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.RunPython(fill, elidable=True),
            """,
            extra="""
            def fill(apps, schema_editor):
                apps.get_model("shop", "Coupon").objects.using(schema_editor.connection.alias).create()
            """,
        )
        self.project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        result = self.project.check_equivalence()
        assert kinds(result) == [("elidable", "shop"), ("model", "shop.Coupon")]
        assert result.removed_operations == 3

    def test_unrelated_elidable_kept(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM + "migrations.RunSQL('SELECT 1', elidable=True),",
        )
        assert not self.project.compact().changed
        result = self.project.check_equivalence(elidable=True)
        assert kinds(result) == [("elidable", "shop")]

    def test_elidable_not_removable_blocks(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM
            + """
            migrations.CreateModel(
                name="Coupon",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_sql",
            source="""
            from django.db import migrations

            OPS = [migrations.RunSQL("DELETE FROM shop_coupon", elidable=True)]


            class Migration(migrations.Migration):
                dependencies = [("shop", "0001_initial")]
                operations = OPS
            """,
        )
        self.project.migration(
            "shop",
            "0003_drop",
            'migrations.DeleteModel("Coupon"),',
            deps=[("shop", "0002_sql")],
        )
        result = self.project.compact()
        assert result.removals == []
        (reason,) = blocked(result)["shop.Coupon"]
        assert reason == (
            "elidable RunSQL in shop.0002_sql mentions 'coupon' but cannot be"
            " removed: operations list in shop.0002_sql could not be parsed"
            " (operations is not a list or tuple literal)"
        )

    def test_elidable_with_state_change_is_opaque(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_sql",
            """
            migrations.RunSQL(
                "SELECT 1",
                state_operations=[
                    migrations.AddField("item", "legacy", models.IntegerField(default=0)),
                ],
                elidable=True,
            ),
            """,
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.compact(elidable=True)
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "created by RunSQL in shop.0002_sql",
        )


class PoisoningTests(ProjectTestCase):
    def test_separate_database_and_state(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_sdas",
            """
            migrations.SeparateDatabaseAndState(
                state_operations=[
                    migrations.CreateModel(
                        name="Ghost",
                        fields=[("id", models.AutoField(primary_key=True))],
                    ),
                ],
                database_operations=[
                    migrations.RunSQL('CREATE TABLE "shop_ghost" ("id" integer NOT NULL PRIMARY KEY AUTOINCREMENT)'),
                ],
            ),
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            """,
        )
        self.project.migration(
            "shop",
            "0003_drop",
            """
            migrations.DeleteModel("Ghost"),
            migrations.RemoveField("item", "legacy"),
            """,
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.legacy")]
        assert blocked(self.project.compact())["shop.Ghost"] == (
            "created by SeparateDatabaseAndState in shop.0002_sdas",
        )

    def test_sdas_touches_existing(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop",
            "0003_sdas",
            """
            migrations.SeparateDatabaseAndState(
                state_operations=[
                    migrations.AlterField("item", "legacy", models.IntegerField(default=5)),
                ],
            ),
            """,
        )
        self.project.migration(
            "shop", "0004_drop", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.compact()
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "changed by SeparateDatabaseAndState in shop.0003_sdas",
        )

    def test_custom_operation(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            """
            MyAddField("item", "legacy", models.IntegerField(default=0)),
            """,
            extra="""
            class MyAddField(migrations.AddField):
                pass
            """,
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.check_equivalence()
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "created by MyAddField in shop.0002_add",
        )

    def test_custom_operation_renaming_model(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_rename",
            'MyRename("Item", "Thing"),',
            extra="""
            class MyRename(migrations.RenameModel):
                pass
            """,
        )
        self.project.migration("shop", "0003_drop", 'migrations.DeleteModel("Thing"),')
        result = self.project.compact()
        assert result.removals == []
        assert blocked(result) == {
            "shop.Item": ("changed by MyRename in shop.0002_rename",),
            "shop.Thing": ("created by MyRename in shop.0002_rename",),
        }


class EditabilityTests(ProjectTestCase):
    def test_unlocatable_operations(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            source="""
            from django.db import migrations, models

            ADD = migrations.AddField("item", "legacy", models.IntegerField(default=0))


            class Migration(migrations.Migration):
                dependencies = [("shop", "0001_initial")]
                operations = [ADD]
            """,
        )
        self.project.migration(
            "shop",
            "0003_drop",
            'migrations.RemoveField("item", "legacy"),',
            deps=[("shop", "0002_add")],
        )
        result = self.project.compact()
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "operations list in shop.0002_add could not be parsed (cannot match AddField in source)",
        )

    def test_unlocatable_sub_element(self):
        self.project.migration(
            "shop",
            "0001_initial",
            source="""
            from django.db import migrations, models

            FIELDS = [
                ("id", models.AutoField(primary_key=True)),
                ("legacy", models.IntegerField()),
            ]


            class Migration(migrations.Migration):
                operations = [
                    migrations.CreateModel(name="Item", fields=FIELDS),
                ]
            """,
        )
        self.project.migration(
            "shop",
            "0002_drop",
            'migrations.RemoveField("item", "legacy"),',
            deps=[("shop", "0001_initial")],
        )
        result = self.project.compact()
        assert blocked(result)["shop.Item.legacy"] == (
            "fields is not a literal in CreateModel in shop.0001_initial",
        )

    def test_non_editable(self):

        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        self.monkeypatch.setattr(
            history,
            "read_only_reason",
            lambda path, root, targeted: (
                "installed package" if path.name == "0002_add.py" else ""
            ),
        )
        result = self.project.compact()
        assert blocked(result)["shop.Item.legacy"] == (
            "shop.0002_add is not editable (installed package)",
        )


class OptionsTests(ProjectTestCase):
    def test_unknown_app(self):
        self.project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="No installed app with label 'nope'"):
            self.project.compact("nope")

    def test_bad_target(self):
        self.project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="Invalid target"):
            self.project.compact("shop.a.b.c")

    def test_bad_kind(self):
        self.project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="Unknown kind"):
            self.project.compact(skip_kinds=["nope"])

    def test_target_matches_any_name(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.RenameField("item", "legacy", "older"),
            migrations.RenameModel("Item", "Product"),
            """,
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.RemoveField("product", "older"),'
        )
        assert len(self.project.compact("SHOP.item.LEGACY").removals) == 1
        assert len(self.project.compact("shop.Product.older").removals) == 1
        assert self.project.compact("shop.Product.name").removals == []
        assert self.project.compact("shop", exclude=["shop.Item.legacy"]).removals == []

    def test_replaced_migrations_warning(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AddField("item", "x", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop",
            "0001_squashed_0002_a",
            source="""
            from django.db import migrations, models


            class Migration(migrations.Migration):
                replaces = [("shop", "0001_initial"), ("shop", "0002_a")]
                operations = [
                    migrations.CreateModel(
                        name="Item",
                        fields=[
                            ("id", models.AutoField(primary_key=True)),
                            ("x", models.IntegerField(default=0)),
                        ],
                    ),
                ]
            """,
        )
        result = self.project.compact()
        assert result.warnings == [
            "2 migration(s) replaced by squashed migrations were skipped:"
            " shop.0001_initial, shop.0002_a"
        ]

    def test_broken_history(self):
        self.project.migration(
            "shop", "0001_initial", 'migrations.DeleteModel("Nope"),'
        )
        with pytest.raises(CompactError, match="refers to the model shop.Nope"):
            self.project.compact()


class UnusualHistoriesTests(ProjectTestCase):
    def test_unordered_branch_in_window(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        # Another app, unordered relative to shop.0002/0003, touches the column.
        self.project.migration(
            "other",
            "0001_initial",
            'migrations.RunSQL("UPDATE shop_item SET legacy = 1"),',
            deps=[("shop", "0001_initial")],
        )
        result = self.project.compact()
        assert blocked(result)["shop.Item.legacy"] == (
            "RunSQL in other.0001_initial mentions 'legacy'",
        )
        # Once ordered after the removal, it is outside the window.
        self.project.migration(
            "other",
            "0001_initial",
            "migrations.RunSQL(\"SELECT 'legacy'\"),",
            deps=[("shop", "0003_remove")],
        )
        assert kinds(self.project.check_equivalence()) == [
            ("field", "shop.Item.legacy")
        ]

    def test_postgres_operations_by_name(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_index",
            """
            AddIndexConcurrently("item", models.Index(fields=["name"], name="n_idx")),
            CreateExtension("whatever"),
            """,
            extra="""
            class AddIndexConcurrently(migrations.AddIndex):
                pass


            class CreateExtension(migrations.operations.base.Operation):
                def __init__(self, name):
                    self.name = name

                def state_forwards(self, app_label, state):
                    pass

                def database_forwards(self, *args):
                    pass


            AddIndexConcurrently.__module__ = "django.contrib.postgres.operations"
            CreateExtension.__module__ = "django.contrib.postgres.operations"
            """,
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.RemoveIndex("item", "n_idx"),'
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("index", "shop.Item")]
        assert "CreateExtension" in self.project.read("shop", "0002_index")

    def test_order_with_respect_to(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM
            + """
            migrations.CreateModel(
                name="Line",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("item", models.ForeignKey("shop.item", models.CASCADE)),
                ],
            ),
            migrations.AlterOrderWithRespectTo("line", "item"),
            """,
        )
        self.project.migration(
            "shop",
            "0002_drop",
            """
            migrations.AlterOrderWithRespectTo("line", None),
            migrations.RemoveField("line", "item"),
            """,
        )
        result = self.project.compact("shop.Line.item")
        assert blocked(result)["shop.Line.item"] == (
            "referenced by AlterOrderWithRespectTo in shop.0001_initial",
        )

    def test_unknown_index_poisons(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Thing",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.RemoveIndex("thing", "nope"),
            """,
        )
        self.project.migration("shop", "0002_drop", 'migrations.DeleteModel("Thing"),')
        result = self.project.compact()
        assert blocked(result)["shop.Thing"] == (
            "unknown index 'nope' in RemoveIndex in shop.0001_initial",
        )

    def test_together_with_dead_field(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            """
            migrations.AddField("item", "x", models.IntegerField(default=0)),
            migrations.AlterUniqueTogether("item", {("name", "x")}),
            """,
        )
        self.project.migration(
            "shop",
            "0003_b",
            """
            migrations.AlterUniqueTogether("item", set()),
            migrations.RemoveField("item", "x"),
            """,
        )
        result = self.project.check_equivalence()
        assert ("field", "shop.Item.x") in kinds(result)
        assert result.empty_migrations == [("shop", "0002_a"), ("shop", "0003_b")]

    def test_together_flat_value(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            """
            migrations.AlterUniqueTogether("item", ("id", "name")),
            migrations.AlterModelOptions("item", {"verbose_name": "x"}),
            """,
        )
        self.project.migration(
            "shop", "0003_b", 'migrations.AlterUniqueTogether("item", None),'
        )
        result = self.project.check_equivalence()
        assert result.removed_operations == 2

    def test_constraint_only_target(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            """
            migrations.AddConstraint(
                "item",
                models.UniqueConstraint(fields=["name"], name="uniq_name"),
            ),
            """,
        )
        self.project.migration(
            "shop", "0003_b", 'migrations.RemoveConstraint("item", "uniq_name"),'
        )
        # A field target doesn't select constraints.
        assert self.project.compact("shop.Item.name").removals == []
        assert kinds(self.project.check_equivalence("shop.Item")) == [
            ("constraint", "shop.Item")
        ]

    def test_redundant_together_unordered_kept(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            """
            migrations.AddField("item", "x", models.IntegerField(default=0)),
            migrations.AlterUniqueTogether("item", {("name", "x")}),
            """,
        )
        self.project.migration(
            "shop", "0003_b", 'migrations.AlterUniqueTogether("item", set()),'
        )
        self.project.migration(
            "shop",
            "0002_c",
            'migrations.AlterUniqueTogether("item", {("id", "name")}),',
            deps=[("shop", "0001_initial")],
        )
        self.project.migration(
            "shop",
            "0004_merge",
            'migrations.RemoveField("item", "x"),',
            deps=[("shop", "0003_b"), ("shop", "0002_c")],
        )
        result = self.project.check_equivalence(**NO_FOLD)
        assert ("field", "shop.Item.x") in kinds(result)
        # 0003_b is unordered relative to 0002_c's together change: kept.
        assert "AlterUniqueTogether" in self.project.read("shop", "0003_b")
        assert result.empty_migrations == [("shop", "0004_merge")]

    def test_dead_model_with_unlocatable_fields(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_coupon",
            source="""
            from django.db import migrations, models

            FIELDS = [("id", models.AutoField(primary_key=True))]


            class Migration(migrations.Migration):
                dependencies = [("shop", "0001_initial")]
                operations = [
                    migrations.CreateModel("Coupon", FIELDS, {"unique_together": UT}),
                ]

            """.replace("UT", "{('id',)}"),
        )
        self.project.migration(
            "shop",
            "0003_drop",
            'migrations.DeleteModel("Coupon"),',
            deps=[("shop", "0002_coupon")],
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("model", "shop.Coupon")]


class CustomStateChangesTests(ProjectTestCase):
    EXTRA = """
    class MyCreateModel(migrations.CreateModel):
        pass


    class MyAddIndex(migrations.AddIndex):
        pass


    class MyAlterUniqueTogether(migrations.AlterUniqueTogether):
        pass


    class MyRemoveConstraint(migrations.RemoveConstraint):
        pass
    """

    def test_custom_create_model(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            MyCreateModel(
                name="Thing",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("a", models.IntegerField()),
                    ("b", models.IntegerField()),
                ],
                options={
                    "indexes": [models.Index(fields=["a"], name="a_idx")],
                    "constraints": [models.CheckConstraint(condition=models.Q(a__gte=0), name="a_pos")],
                    "unique_together": {("a", "b")},
                    "db_table": "thing_table",
                },
            ),
            """,
            extra=self.EXTRA,
        )
        self.project.migration(
            "shop",
            "0002_drop",
            """
            migrations.RemoveIndex("thing", "a_idx"),
            migrations.RemoveConstraint("thing", "a_pos"),
            migrations.AlterUniqueTogether("thing", set()),
            migrations.RemoveField("thing", "b"),
            """,
        )
        result = self.project.check_equivalence()
        assert result.removals == []
        reasons = {r for b in result.blocked for r in b.reasons}
        assert reasons == {"created by MyCreateModel in shop.0001_initial"}
        assert len(result.blocked) == 4

    def test_custom_index_and_together(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_custom",
            """
            migrations.AddField("item", "code", models.IntegerField(default=0)),
            MyAddIndex("item", models.Index(fields=["code"], name="code_idx")),
            MyAlterUniqueTogether("item", {("name", "code")}),
            migrations.AddConstraint(
                "item", models.CheckConstraint(condition=models.Q(code__gte=0), name="c"),
            ),
            MyRemoveConstraint("item", "c"),
            """,
            extra=self.EXTRA,
        )
        self.project.migration(
            "shop",
            "0003_drop",
            """
            migrations.RemoveIndex("item", "code_idx"),
            migrations.AlterUniqueTogether("item", set()),
            migrations.RemoveField("item", "code"),
            """,
        )
        result = self.project.check_equivalence(**NO_FOLD)
        assert result.removals == []
        assert blocked(result) == {
            "shop.Item index 'code_idx'": (
                "created by MyAddIndex in shop.0002_custom",
            ),
            "shop.Item unique_together (name, code)": (
                "created by MyAlterUniqueTogether in shop.0002_custom",
            ),
            "shop.Item constraint 'c'": (
                "changed by MyRemoveConstraint in shop.0002_custom",
            ),
            "shop.Item.code": ("MyAddIndex in shop.0002_custom mentions 'code'",),
        }


def dead_field(project: Project, name: str, ops: str, extra: str = "") -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        f'migrations.AddField("item", "{name}", models.IntegerField(default=0)),\n'
        + ops,
        extra=extra,
    )
    project.migration(
        "shop", "0003_remove", f'migrations.RemoveField("item", "{name}"),'
    )


class ThirdPartyTests(ProjectTestCase):
    def setup(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        deps: list[tuple[str, str]],
    ) -> None:

        dead_field(project, "legacy", "")
        project.migration(
            "vendor",
            "0001_initial",
            "migrations.RunSQL(\"SELECT 'legacy'\"),",
            deps=deps,
        )
        monkeypatch.setattr(
            history,
            "read_only_reason",
            lambda path, root, targeted: (
                "installed package" if "vendor" in path.parts else ""
            ),
        )

    def test_unordered_ignored(self):
        self.setup(self.project, self.monkeypatch, [("shop", "0001_initial")])
        assert kinds(self.project.compact()) == [("field", "shop.Item.legacy")]

    def test_descendant_counts(self):
        self.setup(self.project, self.monkeypatch, [("shop", "0002_add")])
        assert blocked(self.project.compact())["shop.Item.legacy"] == (
            "RunSQL in vendor.0001_initial mentions 'legacy'",
        )


class PoisoningWidthTests(ProjectTestCase):
    def test_sdas_index_poisons_only_index(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_index",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.SeparateDatabaseAndState(
                state_operations=[
                    migrations.AddIndex("item", models.Index(fields=["name"], name="n_idx")),
                ],
                database_operations=[
                    migrations.RunSQL('CREATE INDEX "n_idx" ON "shop_item" ("name")'),
                ],
            ),
            migrations.AlterModelOptions("item", {"verbose_name": "a"}),
            migrations.AlterModelOptions("item", {"verbose_name": "b"}),
            """,
        )
        self.project.migration(
            "shop",
            "0003_drop",
            """
            migrations.RemoveField("item", "legacy"),
            migrations.RemoveIndex("item", "n_idx"),
            migrations.DeleteModel("Item"),
            """,
        )
        result = self.project.compact()
        assert kinds(result) == [
            ("field", "shop.Item.legacy"),
            ("superseded", "shop.Item"),
        ]
        assert blocked(result) == {
            "shop.Item": (
                "its index shop.Item 'n_idx' was created by SeparateDatabaseAndState"
                " in shop.0002_index",
            ),
            "shop.Item index 'n_idx'": (
                "created by SeparateDatabaseAndState in shop.0002_index",
            ),
        }
        self.project.check_equivalence()

    def test_state_only_remove_field(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop",
            "0003_unmount",
            """
            migrations.SeparateDatabaseAndState(
                state_operations=[migrations.RemoveField("item", "legacy")],
            ),
            migrations.AlterUniqueTogether("item", {("id", "name")}),
            migrations.SeparateDatabaseAndState(
                state_operations=[migrations.AlterUniqueTogether("item", set())],
            ),
            """,
        )
        result = self.project.compact()
        assert blocked(result)["shop.Item.legacy"] == (
            "ended by SeparateDatabaseAndState in shop.0003_unmount",
        )
        assert "shop.Item" not in blocked(result)


NETBOX_OPTIONS = """
migrations.AlterModelOptions("item", {"ordering": ORDERING}),
migrations.AddField("item", "_name", models.CharField(blank=True, default="", max_length=100)),
"""


class SoftReferencesTests(ProjectTestCase):
    def history(self, project: Project, ordering: str, query: str) -> None:
        project.migration("shop", "0001_initial", ITEM)
        # The ordering names _name before the field exists (netbox shape).
        project.migration(
            "shop", "0002_name", NETBOX_OPTIONS.replace("ORDERING", ordering)
        )
        project.migration(
            "shop",
            "0003_data",
            "migrations.RunPython(forwards),",
            extra=f"""
            def forwards(apps, schema_editor):
                alias = schema_editor.connection.alias
                {query}
            """,
        )
        project.migration(
            "shop",
            "0004_natural",
            """
            migrations.AlterModelOptions("item", {"ordering": ("name", "pk")}),
            migrations.RemoveField("item", "_name"),
            """,
        )

    def test_netbox_shape(self):
        self.history(
            self.project,
            "('_name', 'pk')",
            'list(apps.get_model("shop", "Item").objects.using(alias).all())',
        )
        result = self.project.check_equivalence(skip_kinds=["fold"])
        assert result.removals == []
        assert blocked(result)["shop.Item._name"] == (
            "AlterModelOptions in shop.0002_name orders by it, and meanwhile"
            " RunPython in shop.0003_data mentions 'item'",
        )

    @parametrize(
        "ordering",
        ["['-_name']", "(models.F('_name').asc(),)", "['_name__lower']"],
    )
    def test_ordering_forms(self, ordering):
        self.history(self.project, ordering, 'apps.get_model("shop", "Item")')
        assert "shop.Item._name" in blocked(self.project.compact())

    def test_get_latest_by(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_name",
            """
            migrations.AddField("item", "stamp", models.IntegerField(default=0)),
            migrations.AlterModelOptions("item", {"get_latest_by": ["stamp"]}),
            migrations.RunSQL("SELECT 1 FROM shop_item"),
            migrations.AlterModelOptions("item", {}),
            migrations.RemoveField("item", "stamp"),
            """,
        )
        assert "shop.Item.stamp" in blocked(self.project.compact())

    def test_period_outside_query(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_name",
            """
            migrations.AddField("item", "_name", models.CharField(default="", max_length=5)),
            migrations.AlterModelOptions("item", {"ordering": ["_name"]}),
            migrations.AlterModelOptions("item", {"ordering": ["name"]}),
            migrations.RunPython(forwards),
            migrations.RemoveField("item", "_name"),
            """,
            extra="""
            def forwards(apps, schema_editor):
                alias = schema_editor.connection.alias
                list(apps.get_model("shop", "Item").objects.using(alias).all())
            """,
        )
        assert kinds(self.project.check_equivalence("shop.Item._name")) == [
            ("field", "shop.Item._name")
        ]

    def test_query_through_many_to_many(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_name",
            NETBOX_OPTIONS.replace("ORDERING", "['_name']")
            + """
            migrations.CreateModel(
                name="Box",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("stuff", models.ManyToManyField("shop.item")),
                ],
            ),
            migrations.RunPython(forwards),
            """,
            extra="""
            def forwards(apps, schema_editor):
                for box in apps.get_model("shop", "Box").objects.all():
                    list(box.stuff.all())
            """,
        )
        self.project.migration(
            "shop",
            "0003_drop",
            """
            migrations.AlterModelOptions("item", {}),
            migrations.RemoveField("item", "_name"),
            """,
        )
        assert "shop.Item._name" in blocked(
            self.project.compact(skip_kinds=["fold", "superseded"])
        )

    def test_query_through_child_model(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_name",
            NETBOX_OPTIONS.replace("ORDERING", "['_name']")
            + """
            migrations.CreateModel(
                name="Special",
                fields=[
                    ("item_ptr", models.OneToOneField(auto_created=True, on_delete=models.CASCADE, parent_link=True, primary_key=True, serialize=False, to="shop.item")),
                ],
                bases=("shop.item",),
            ),
            migrations.RunSQL("SELECT 1 FROM shop_special"),
            """,
        )
        self.project.migration(
            "shop",
            "0003_drop",
            """
            migrations.AlterModelOptions("item", {}),
            migrations.RemoveField("item", "_name"),
            """,
        )
        assert "shop.Item._name" in blocked(
            self.project.compact(skip_kinds=["fold", "superseded"])
        )


class PrecisionTests(ProjectTestCase):
    def test_blocked_lines_name_their_migrations(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "body", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "body"),'
        )
        self.project.migration(
            "shop",
            "0004_add",
            """
            migrations.AddField("item", "body", models.IntegerField(default=0)),
            migrations.RunSQL("SELECT body FROM shop_item"),
            """,
        )
        self.project.migration(
            "shop", "0005_remove", 'migrations.RemoveField("item", "body"),'
        )
        # Without folds: the kept AddField would fold into the CreateModel, and
        # the blocked field be reported as added there.
        out, _ = self.project.command(
            "plan", "--static", "-v", "2", "--skip-kind", "fold"
        )
        lines = out.splitlines()
        assert lines[2].split() == [
            "body",
            "field",
            "0002_add",
            "->",
            "0003_remove",
            "2",
            "operations",
        ]
        blocked = lines.index("Blocked")
        assert lines[blocked + 1].split(None, 1) == [
            "shop.Item.body",
            "field added in 0004_add, removed in 0005_remove",
        ]
        assert lines[blocked + 2] == "    RunSQL in shop.0004_add mentions 'body'"
