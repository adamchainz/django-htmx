from __future__ import annotations

import tomllib

from unittest_parametrize import parametrize

from django_compact_migrations.core import Result
from django_compact_migrations.fold import _mentions
from tests.project import (
    ITEM,
    Project,
    ProjectTestCase,
    blocked,
    folds,
    kinds,
)


class DatabaseNamesTests(ProjectTestCase):
    """Folds that would change the names of database objects are refused."""

    def test_unique_added_by_alter_field(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_unique",
            'migrations.AlterField("item", "name", models.CharField(max_length=10, unique=True)),',
        )
        assert folds(self.project.check_equivalence()) == []

    def test_check_added_by_alter_field(self):
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
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_positive",
            'migrations.AlterField("item", "qty", models.PositiveIntegerField()),',
        )
        assert folds(self.project.check_equivalence()) == []

    def test_alter_field_folds_under_create_model_index(self):
        # The static plan folds the index into the CreateModel. There it
        # names the field, but is no field definition pointing at it, so the
        # trial can still fold the AlterField that the name scan blocks.
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("code", models.CharField(max_length=10)),
                ],
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_count",
            "migrations.RunPython(count, migrations.RunPython.noop),",
            extra="""
            def count(apps, schema_editor):
                alias = schema_editor.connection.alias
                apps.get_model("shop", "Item").objects.using(alias).filter(code="").count()
            """,
        )
        self.project.migration(
            "shop",
            "0003_longer",
            'migrations.AlterField("item", "code", models.CharField(max_length=20)),',
        )
        self.project.migration(
            "shop",
            "0004_index",
            """
            migrations.AddIndex(
                "item", models.Index(fields=["code"], name="item_code_idx")
            ),
            """,
        )
        index = (
            "shop.Item",
            "AddIndex in 0004_index folded into CreateModel in 0001_initial",
        )
        assert folds(self.project.compact()) == [index]
        assert folds(self.project.compact(static=False)) == [
            index,
            (
                "shop.Item.code",
                "AlterField in 0003_longer folded into CreateModel in 0001_initial",
            ),
        ]

    @parametrize("custom", [True, False, "collation"])
    def test_custom_field_not_folded_across_setup(self, custom):
        self.project.migration("shop", "0001_initial", ITEM)
        (self.project.path("shop").parent / "fields.py").write_text(
            "from django.db import models\n\n\nclass Tagged(models.TextField):\n"
            "    pass\n"
        )
        field = "Tagged" if custom is True else "models.TextField"
        if custom == "collation":
            field = 'functools.partial(models.TextField, db_collation="nocase")'

        self.project.migration(
            "shop",
            "0002_tags",
            f"""
            migrations.RunSQL("SELECT 1"),
            migrations.AddField("item", "tags", {field}(default="")),
            """,
            extra=f"import functools\nfrom {self.project.package}.shop.fields import Tagged",
        )
        assert bool(folds(self.project.check_equivalence())) is not bool(custom)

    @parametrize(
        "setup", ["HStoreExtension()", 'CreateCollation("natural", "und-u-kn-true")']
    )
    def test_postgres_setup_operations(self, setup):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_tags",
            f"""
            {setup},
            migrations.AddField(
                "item", "tags", models.TextField(default="", db_collation="nocase")
            ),
            """,
            extra="from django.contrib.postgres.operations import"
            " CreateCollation, HStoreExtension",
        )
        assert folds(self.project.check_equivalence()) == []

    def test_same_check_folds(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_qty",
            'migrations.AddField("item", "qty", models.PositiveIntegerField(default=1)),',
        )
        self.project.migration(
            "shop",
            "0003_qty",
            'migrations.AlterField("item", "qty", models.PositiveIntegerField(default=2)),',
        )
        assert self.project.check_equivalence().folded_operations == 2

    def test_m2m_before_pk_alter(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(name="Tag", fields=[("id", models.AutoField(primary_key=True))]),
            """
            + ITEM,
        )
        self.project.migration(
            "shop",
            "0002_bigint",
            'migrations.AlterField("tag", "id", models.BigAutoField(primary_key=True)),',
        )
        self.project.migration(
            "shop",
            "0003_tags",
            'migrations.AddField("item", "tags", models.ManyToManyField("shop.tag")),',
        )
        assert folds(self.project.check_equivalence()) == []

    def test_fk_target_altered_in_window(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(name="Tag", fields=[("id", models.AutoField(primary_key=True))]),
            """
            + ITEM,
        )
        self.project.migration(
            "shop",
            "0002_bigint",
            'migrations.AlterField("tag", "id", models.BigAutoField(primary_key=True)),',
        )
        self.project.migration(
            "shop",
            "0003_tag",
            'migrations.AddField("item", "tag", models.ForeignKey("shop.tag", models.CASCADE, null=True)),',
        )
        assert folds(self.project.check_equivalence()) == []

    def test_table_renamed_in_window(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop", "0002_table", 'migrations.AlterModelTable("item", "things"),'
        )
        self.project.migration(
            "shop",
            "0003_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1, unique=True)),',
        )
        assert folds(self.project.check_equivalence()) == []

    def test_superseded_unique_variation(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.CharField(max_length=10, unique=True)),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "name", models.CharField(max_length=20, unique=True)),',
        )
        result = self.project.check_equivalence(skip_kinds=["fold"])
        assert result.removals == []

    def test_to_field_target_not_altered_by_fold(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("code", models.CharField(max_length=10, unique=True)),
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
            "0002_code",
            'migrations.AlterField("item", "code", models.CharField(max_length=20, unique=True)),',
        )
        result = self.project.check_equivalence()
        assert not any(label == "shop.Item.code" for label, _ in folds(result))


class RenamesTests(ProjectTestCase):
    """RenameField folds into the field's creation; renamed database
    objects keep names derived from the old name, so most can't fold."""

    def renamed(self, project: Project, field: str, *, extra_ops: str = "") -> Result:
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop", "0002_qty", f'migrations.AddField("item", "qty", {field}),'
        )
        project.migration(
            "shop",
            "0003_rename",
            extra_ops + 'migrations.RenameField("item", "qty", "quantity"),',
        )
        return project.check_equivalence()

    def test_folded_into_add_field(self):
        result = self.renamed(self.project, "models.IntegerField(default=1)")
        descriptions = [d for _, d in folds(result)]
        # Into the AddField, which then goes into the CreateModel.
        assert "RenameField in 0003_rename folded into CreateModel in 0001_initial" in (
            descriptions
        )
        assert "RenameField" not in self.project.read("shop", "0003_rename")

    @parametrize(
        "field",
        [
            "models.IntegerField(default=1, unique=True)",
            "models.IntegerField(default=1, db_index=True)",
            "models.PositiveIntegerField(default=1)",
            "models.ForeignKey('shop.Item', models.CASCADE, null=True)",
            "models.ManyToManyField('shop.Item')",
        ],
    )
    def test_named_objects_block(self, field):
        result = self.renamed(self.project, field)
        assert not [d for _, d in folds(result) if d.startswith("RenameField")]

    def test_db_column_keeps_names(self):
        result = self.renamed(
            self.project, "models.IntegerField(default=1, unique=True, db_column='q')"
        )
        assert [d for _, d in folds(result) if d.startswith("RenameField")]

    def test_reference_between_blocks(self):
        result = self.renamed(
            self.project,
            "models.IntegerField(default=1)",
            extra_ops='migrations.AddIndex("item", models.Index(fields=["qty"], name="qty_idx")),\n',
        )
        assert not [d for _, d in folds(result) if d.startswith("RenameField")]

    def test_opaque_mention_blocks(self):
        result = self.renamed(
            self.project,
            "models.IntegerField(default=1)",
            extra_ops='migrations.RunSQL("UPDATE shop_item SET qty = 2"),\n',
        )
        assert not [d for _, d in folds(result) if d.startswith("RenameField")]

    def test_not_last_field_blocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
        )
        self.project.migration(
            "shop", "0003_rename", 'migrations.RenameField("item", "name", "title"),'
        )
        result = self.project.check_equivalence()
        assert not [d for _, d in folds(result) if d.startswith("RenameField")]

    def test_create_model_last_entry(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop", "0002_rename", 'migrations.RenameField("item", "name", "title"),'
        )
        result = self.project.check_equivalence()
        assert [d for _, d in folds(result) if d.startswith("RenameField")]
        assert "'title'" in self.project.read("shop", "0001_initial")

    def test_field_pointed_at_by_to_field(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop", "0002_rename", 'migrations.RenameField("item", "name", "title"),'
        )
        self.project.migration(
            "shop",
            "0003_tag",
            """
            migrations.CreateModel(
                name="Tag",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    (
                        "item",
                        models.ForeignKey(
                            "shop.item", models.CASCADE, to_field="title"
                        ),
                    ),
                ],
            ),
            """,
        )
        result = self.project.compact()
        assert not [d for _, d in folds(result) if d.startswith("RenameField")]

    def test_new_name_taken_before_blocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_old",
            'migrations.AddField("item", "quantity", models.IntegerField(default=1)),',
        )
        self.project.migration(
            "shop",
            "0003_drop",
            # Keeps the old field from being removed as dead.
            'migrations.RunSQL("SELECT quantity FROM shop_item"),\n'
            'migrations.RemoveField("item", "quantity"),',
        )
        self.project.migration(
            "shop",
            "0004_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
        )
        self.project.migration(
            "shop",
            "0005_rename",
            'migrations.RenameField("item", "qty", "quantity"),',
        )
        result = self.project.check_equivalence()
        assert not [d for _, d in folds(result) if d.startswith("RenameField in 0005")]

    def test_unmanaged_table_folded(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="View",
                fields=[("id", models.AutoField(primary_key=True))],
                options={"managed": False},
            ),
            """,
        )
        self.project.migration(
            "shop", "0002_table", 'migrations.AlterModelTable("view", "some_view"),'
        )
        result = self.project.check_equivalence()
        assert [d for _, d in folds(result) if d.startswith("AlterModelTable")]
        assert "some_view" in self.project.read("shop", "0001_initial")

    def test_managed_table_not_folded(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop", "0002_table", 'migrations.AlterModelTable("item", "goods"),'
        )
        result = self.project.check_equivalence()
        assert not [d for _, d in folds(result) if d.startswith("AlterModelTable")]


def unordered(project: Project, first: str, second: str) -> None:
    """
    Two migrations of the app on parallel branches, merged afterwards
    (Django happens to run the second branch first).
    """
    project.migration("shop", "0002_b", first, deps=[("shop", "0001_initial")])
    project.migration("shop", "0002_a", second, deps=[("shop", "0001_initial")])
    project.migration(
        "shop", "0003_merge", deps=[("shop", "0002_a"), ("shop", "0002_b")]
    )


# A RunSQL naming the model keeps the AddField out of the CreateModel.
KEPT_QTY = """
migrations.RunSQL("SELECT 'item'"),
migrations.AddField("item", "qty", models.IntegerField(default=1)),
"""

UNMANAGED = """
migrations.CreateModel(
    name="Item",
    fields=[("id", models.AutoField(primary_key=True))],
    options={"managed": False},
),
"""

ITEM_CODE = """
migrations.CreateModel(
    name="Item",
    fields=[
        ("id", models.AutoField(primary_key=True)),
        ("name", models.CharField(max_length=10)),
        ("code", models.CharField(max_length=10)),
    ],
),
"""


class GuardsTests(ProjectTestCase):
    """Each condition that keeps an operation where it is."""

    @parametrize(
        ("initial", "first", "second"),
        [
            (
                ITEM,
                KEPT_QTY,
                'migrations.AlterField("item", "qty", models.IntegerField(default=2)),',
            ),
            (ITEM, KEPT_QTY, 'migrations.RenameField("item", "qty", "quantity"),'),
            (
                "",
                ITEM,
                'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
            ),
            (
                "",
                ITEM,
                'migrations.AddIndex("item", models.Index(fields=["name"], name="n_idx")),',
            ),
            ("", ITEM, 'migrations.AlterUniqueTogether("item", {("id", "name")}),'),
            ("", ITEM, 'migrations.AlterModelOptions("item", {"verbose_name": "x"}),'),
            ("", UNMANAGED, 'migrations.AlterModelTable("item", "things"),'),
        ],
    )
    def test_unordered_branches(self, initial, first, second):
        self.project.migration("shop", "0001_initial", initial)
        unordered(self.project, first, second)
        result = self.project.compact()
        assert not [d for _, d in folds(result) if "0002_a" in d]

    def test_unparsable_together(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_together",
            source="from django.db import migrations\n\n\n"
            "class Migration(migrations.Migration):\n"
            "    dependencies = [('shop', '0001_initial')]\n"
            "    operations = ops = [\n"
            '        migrations.AlterUniqueTogether("item", {("id", "name")}),\n'
            "    ]\n",
        )
        assert folds(self.project.compact()) == []

    def test_model_created_with_removed_field(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("legacy", models.IntegerField()),
                ],
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "a", models.IntegerField(default=1)),
            migrations.AddField("item", "b", models.IntegerField(default=1)),
            migrations.RenameField("item", "b", "c"),
            migrations.AddField("item", "d", models.IntegerField(default=1)),
            migrations.RenameField("item", "a", "e"),
            """,
        )
        self.project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.check_equivalence()
        into_create = "AddField in 0002_add folded into CreateModel in 0001_initial"
        assert folds(result) == [
            ("shop.Item.c", into_create),
            ("shop.Item.c", "RenameField in 0002_add folded into AddField in 0002_add"),
            ("shop.Item.d", into_create),
            ("shop.Item.e", into_create),
        ]

    def test_order_with_respect_to(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("parent", models.ForeignKey("shop.item", models.CASCADE, null=True)),
                ],
                options={"order_with_respect_to": "parent"},
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
        )
        assert folds(self.project.check_equivalence()) == []

    def test_one_off_default_into_create_model(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1),'
            " preserve_default=False),",
        )
        self.project.check_equivalence()
        assert "('qty', models.IntegerField())," in self.project.read(
            "shop", "0001_initial"
        )

    def test_renamed_sibling_blocks_add_field(self):
        self.project.migration("shop", "0001_initial", ITEM_CODE)
        self.project.migration(
            "shop", "0002_rename", 'migrations.RenameField("item", "name", "title"),'
        )
        self.project.migration(
            "shop",
            "0003_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
        )
        # Nor does the together option go in, naming a field that doesn't.
        self.project.migration(
            "shop",
            "0004_together",
            'migrations.AlterUniqueTogether("item", {("title", "qty")}),',
        )
        assert folds(self.project.check_equivalence()) == []

    def test_index_on_field_left_out(self):
        self.project.migration("shop", "0001_initial", ITEM_CODE)
        self.project.migration(
            "shop", "0002_rename", 'migrations.RenameField("item", "name", "title"),'
        )
        self.project.migration(
            "shop",
            "0003_qty",
            """
            migrations.AddField("item", "qty", models.IntegerField(default=1)),
            migrations.AddIndex("item", models.Index(fields=["qty"], name="q_idx")),
            """,
        )
        result = self.project.check_equivalence()
        assert not [d for _, d in folds(result) if d.startswith("AddIndex")]

    @parametrize(
        ("added", "between", "altered"),
        [
            # Many-to-many fields have no column to fold a change into.
            (
                'models.ManyToManyField("shop.tag")',
                "",
                'models.ManyToManyField("shop.tag", blank=True)',
            ),
            # The column now follows another field's type.
            (
                'models.ForeignKey("shop.tag", models.CASCADE, to_field="code", null=True)',
                "",
                'models.ForeignKey("shop.tag", models.CASCADE, null=True)',
            ),
            # The table is renamed in between.
            (
                "models.IntegerField(default=1)",
                'migrations.AlterModelTable("item", "things"),',
                "models.IntegerField(default=2)",
            ),
            # An expression no longer names another field.
            (
                'models.CharField(max_length=5, db_default=Upper(F("name")))',
                "",
                'models.CharField(max_length=5, db_default="x")',
            ),
            # The new target's primary key changes in between.
            (
                'models.ForeignKey("shop.tag", models.CASCADE, null=True)',
                'migrations.AlterField("other", "id", models.AutoField(primary_key=True, verbose_name="i")),',
                'models.ForeignKey("shop.other", models.CASCADE, null=True)',
            ),
            # Something names the field in between.
            (
                "models.IntegerField(default=1)",
                'migrations.AddIndex("item", models.Index(fields=["it"], name="it_idx")),',
                "models.IntegerField(default=2)",
            ),
            # A collation may be created in between.
            (
                "models.TextField(default='')",
                'CreateCollation("nocase", "und-u-ks-level2"),',
                "models.TextField(default='', db_collation='nocase')",
            ),
            # An opaque operation may use the column in between.
            (
                "models.IntegerField(default=1)",
                'migrations.RunSQL("UPDATE shop_item SET it = 2"),',
                "models.IntegerField(default=2)",
            ),
        ],
    )
    def test_alter_field_kept(self, added, between, altered):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Tag",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("code", models.CharField(max_length=5, unique=True)),
                ],
            ),
            migrations.CreateModel(
                name="Other", fields=[("id", models.AutoField(primary_key=True))]
            ),
            """
            + ITEM,
        )
        # Kept out of the CreateModel by the RunSQL.
        self.project.migration(
            "shop",
            "0002_it",
            f"""
            migrations.RunSQL("SELECT 'item'"),
            migrations.AddField("item", "it", {added}),
            {between}
            """,
            extra="from django.contrib.postgres.operations import CreateCollation\n"
            "from django.db.models import F\n"
            "from django.db.models.functions import Upper",
        )
        self.project.migration(
            "shop", "0003_alter", f'migrations.AlterField("item", "it", {altered}),'
        )
        result = self.project.compact()
        assert not [d for _, d in folds(result) if d.startswith("AlterField")]

    def test_together_cleared(self):
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
                options={"index_together": TOGETHER},
            ),
            """,
            extra='TOGETHER = {("id", "name")}',
        )
        # The entry can't be located to remove it as dead: the clearing
        # folds instead.
        self.project.migration(
            "shop",
            "0002_together",
            'migrations.AlterIndexTogether("item", set()),',
        )
        result = self.project.check_equivalence()
        assert [d for _, d in folds(result)] == [
            "AlterIndexTogether in 0002_together folded into CreateModel in 0001_initial"
        ]
        assert "index_together" not in self.project.read("shop", "0001_initial")

    @parametrize("same_migration", [True, False])
    def test_target_deleted_later(self, same_migration):
        later = """
            migrations.RunSQL("SELECT tag_id FROM shop_item, shop_tag"),
            migrations.RemoveField("item", "tag"),
            migrations.DeleteModel("Tag"),
        """
        add = 'migrations.AddField("item", "tag", models.ForeignKey("shop.tag", models.CASCADE, null=True)),'
        tag = 'migrations.CreateModel(name="Tag", fields=[("id", models.AutoField(primary_key=True))]),'
        if same_migration:
            self.project.migration("shop", "0001_initial", tag + ITEM + add + later)
        else:
            self.project.migration("shop", "0001_initial", tag + ITEM)
            self.project.migration("shop", "0002_tag", add)
            self.project.migration("shop", "0003_drop", later)
        result = self.project.check_equivalence()
        assert [label for label, _ in folds(result)] == ["shop.Item.tag"]

    @parametrize(
        "operation",
        [
            'migrations.AlterUniqueTogether("item", {("id", "name")})',
            'migrations.AlterModelOptions("item", {"verbose_name": "x"})',
        ],
    )
    def test_opaque_in_window(self, operation):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_change",
            f'migrations.RunSQL("SELECT 1 FROM shop_item"),\n{operation},',
        )
        assert folds(self.project.compact()) == []

    def test_rename_named_in_options(self):
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
                options={"ordering": ["name"]},
            ),
            """,
        )
        self.project.migration(
            "shop", "0002_rename", 'migrations.RenameField("item", "name", "title"),'
        )
        assert folds(self.project.check_equivalence()) == []

    def test_rename_after_removed_sibling(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_legacy",
            'migrations.AddField("item", "legacy", models.IntegerField(default=1)),',
        )
        self.project.migration(
            "shop", "0003_rename", 'migrations.RenameField("item", "name", "title"),'
        )
        self.project.migration(
            "shop", "0004_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.check_equivalence()
        assert [d for _, d in folds(result)] == [
            "RenameField in 0003_rename folded into CreateModel in 0001_initial"
        ]

    def test_custom_field_rename(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("tags", ArrayField(models.IntegerField(), default=list)),
                ],
            ),
            """,
            extra="from django.contrib.postgres.fields import ArrayField",
        )
        self.project.migration(
            "shop", "0002_rename", 'migrations.RenameField("item", "tags", "labels"),'
        )
        assert folds(self.project.compact()) == []

    @parametrize(
        ("between", "table", "folded"),
        [
            ("", '"t"', True),
            ("", "None", False),
            # (An AlterModelOptions would make it managed: the model is
            # then locked.)
            (
                'migrations.AlterModelManagers("item", [("objects", models.Manager())]),',
                '"t"',
                False,
            ),
            ('migrations.RunSQL("SELECT 1 FROM item"),', '"t"', False),
        ],
    )
    def test_unmanaged_table(self, between, table, folded):
        self.project.migration(
            "shop",
            "0001_initial",
            UNMANAGED.replace('"managed": False', '"managed": False, "db_table": "x"'),
        )
        self.project.migration(
            "shop",
            "0002_table",
            f'{between}\nmigrations.AlterModelTable("item", {table}),',
        )
        result = self.project.compact()
        assert (
            bool([d for _, d in folds(result) if d.startswith("AlterModelTable")])
            is folded
        )

    def test_managed_changed_by_options(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_unmanaged",
            'migrations.AlterModelOptions("item", {"managed": False}),',
        )
        assert folds(self.project.compact()) == []


# Rows written by data migrations before the fold point (safety review A1)

SEED = """
def seed(apps, schema_editor):
    alias = schema_editor.connection.alias
    apps.get_model("shop", "Item").objects.using(alias).create(name="a")
"""


def seeded(project: Project, add: str, alter: str) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_seed", "migrations.RunPython(seed),", extra=SEED)
    project.migration("shop", "0003_add", f'migrations.AddField("item", "qty", {add}),')
    project.migration(
        "shop", "0004_alter", f'migrations.AlterField("item", "qty", {alter}),'
    )


class FoldTests(ProjectTestCase):
    def test_add_field_into_create_model(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
        )
        result = self.project.check_equivalence()
        assert folds(result) == [
            (
                "shop.Item.qty",
                "AddField in 0002_qty folded into CreateModel in 0001_initial",
            )
        ]
        assert result.folded_operations == 1
        assert result.removed_operations == 0
        assert result.edited_operations == 1
        assert self.project.read("shop", "0001_initial").endswith(
            "    operations = [\n"
            "        migrations.CreateModel(\n"
            "            name='Item',\n"
            "            fields=[\n"
            "                ('id', models.AutoField(primary_key=True)),\n"
            "                ('name', models.CharField(max_length=10)),\n"
            "                ('qty', models.IntegerField(default=1)),\n"
            "            ],\n"
            "        ),\n"
            "    ]\n"
        )
        assert "operations = []" in self.project.read("shop", "0002_qty")

    def test_alter_field_into_add_field_with_one_off_default(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_qty",
            """
            migrations.RunSQL("INSERT INTO shop_item (name) VALUES ('a')"),
            migrations.AddField("item", "qty", models.IntegerField(default=5), preserve_default=False),
            """,
        )
        self.project.migration(
            "shop",
            "0003_qty",
            'migrations.AlterField("item", "qty", models.IntegerField(null=True)),',
        )
        result = self.project.check_equivalence()
        assert folds(result) == [
            (
                "shop.Item.qty",
                "AlterField in 0003_qty folded into AddField in 0002_qty",
            )
        ]
        source = self.project.read("shop", "0002_qty")
        assert "migrations.AddField(\n" in source
        assert "field=models.IntegerField(default=5, null=True)," in source
        assert "preserve_default=False," in source
        assert "operations = []" in self.project.read("shop", "0003_qty")

    def test_chain_into_create_model(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
        )
        self.project.migration(
            "shop",
            "0003_qty",
            'migrations.AlterField("item", "qty", models.BigIntegerField(default=2)),',
        )
        result = self.project.check_equivalence()
        assert result.folded_operations == 2
        assert result.empty_migrations == [("shop", "0002_qty"), ("shop", "0003_qty")]
        assert "('qty', models.BigIntegerField(default=2))," in self.project.read(
            "shop", "0001_initial"
        )

    def test_order_stops_at_first_unfoldable(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_tag",
            """
            migrations.CreateModel(
                name="Tag",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            migrations.AddField("item", "tag", models.ForeignKey("shop.tag", models.CASCADE, null=True)),
            migrations.AddField("item", "qty", models.IntegerField(default=1)),
            """,
        )
        result = self.project.check_equivalence()
        # tag can't move before Tag exists, and qty can't jump the queue.
        assert folds(result) == []

    def test_retarget_makes_model_dead(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM
            + """
            migrations.CreateModel(name="Old", fields=[("id", models.AutoField(primary_key=True))]),
            migrations.CreateModel(name="New", fields=[("id", models.AutoField(primary_key=True))]),
            """,
        )
        self.project.migration(
            "shop",
            "0002_fk",
            'migrations.AddField("item", "ref", models.ForeignKey("shop.old", models.CASCADE, null=True)),',
        )
        self.project.migration(
            "shop",
            "0003_fk",
            'migrations.AlterField("item", "ref", models.ForeignKey("shop.new", models.CASCADE, null=True)),',
        )
        self.project.migration("shop", "0004_drop", 'migrations.DeleteModel("Old"),')
        result = self.project.check_equivalence()
        assert ("model", "shop.Old") in kinds(result)
        # The AddField can't move into Item's CreateModel: New comes after it.
        assert folds(result) == [
            ("shop.Item.ref", "AlterField in 0003_fk folded into AddField in 0002_fk")
        ]

    def test_retarget_to_later_model_blocked(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM
            + """
            migrations.CreateModel(name="Old", fields=[("id", models.AutoField(primary_key=True))]),
            migrations.AddField("item", "ref", models.ForeignKey("shop.old", models.CASCADE, null=True)),
            """,
        )
        self.project.migration(
            "shop",
            "0002_fk",
            """
            migrations.CreateModel(name="New", fields=[("id", models.AutoField(primary_key=True))]),
            migrations.AlterField("item", "ref", models.ForeignKey("shop.new", models.CASCADE, null=True)),
            """,
        )
        result = self.project.check_equivalence()
        assert (
            "shop.Item.ref",
            "AlterField in 0002_fk folded into AddField in 0001_initial",
        ) not in folds(result)

    def test_column_collision_blocked(self):
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
        result = self.project.check_equivalence()
        assert not any(
            label == "shop.Item.a" and "AlterField" in d for label, d in folds(result)
        )

    def test_opaque_scan_blocks_and_trial_unlocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_data",
            "migrations.RunPython(forwards),",
            extra="""
            def forwards(apps, schema_editor):
                alias = schema_editor.connection.alias
                for item in apps.get_model("shop", "Item").objects.using(alias).all():
                    item.save()
            """,
        )
        self.project.migration(
            "shop",
            "0003_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
        )
        assert folds(self.project.compact()) == []
        result = self.project.check_equivalence(static=False)
        assert [label for label, _ in folds(result)] == ["shop.Item.qty"]
        assert result.verification["unlocked"] == 1

    def test_imports_added(self):
        self.project.migration(
            "shop",
            "0001_initial",
            source="""
            from django.db import migrations, models


            class Migration(migrations.Migration):
                operations = [
                    migrations.CreateModel(
                        name="Tag",
                        fields=[("id", models.AutoField(primary_key=True))],
                    ),
                    migrations.CreateModel(
                        name="Item",
                        fields=[("id", models.AutoField(primary_key=True))],
                    ),
                ]
            """,
        )
        self.project.migration(
            "shop",
            "0002_tag",
            'migrations.AddField("item", "tag", models.ForeignKey("shop.tag", models.CASCADE)),',
            deps=[("shop", "0001_initial")],
        )
        self.project.check_equivalence()
        assert self.project.read("shop", "0001_initial").startswith(
            "from django.db import migrations, models\nimport django.db.models.deletion\n"
        )

    def test_import_clash_blocks(self):
        self.project.migration(
            "shop",
            "0001_initial",
            source="""
            from django.db import migrations
            import django.db.models as models


            class Migration(migrations.Migration):
                operations = [
                    migrations.CreateModel(
                        name="Item",
                        fields=[("id", models.AutoField(primary_key=True))],
                    ),
                ]
            """,
        )
        self.project.migration(
            "shop",
            "0002_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
            deps=[("shop", "0001_initial")],
        )
        assert folds(self.project.check_equivalence()) == []

    def test_unserializable_blocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=make_default)),',
            extra="""
            def make_default():
                return 1
            """,
        )
        assert folds(self.project.check_equivalence()) == []

    def test_index_and_constraint(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_idx",
            """
            migrations.AddIndex("item", models.Index(fields=["name"], name="name_idx")),
            migrations.AddConstraint("item", models.CheckConstraint(condition=models.Q(name__gt=""), name="name_set")),
            """,
        )
        result = self.project.check_equivalence()
        assert sorted(r.name for r in result.removals) == ["name_idx", "name_set"]
        source = self.project.read("shop", "0001_initial")
        assert "'indexes': [models.Index(fields=['name'], name='name_idx')]," in source

    def test_constraint_scan_uses_model_names(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_idx",
            """
            migrations.RunSQL("INSERT INTO shop_item (name) VALUES ('')"),
            migrations.AddConstraint("item", models.CheckConstraint(condition=models.Q(name__gt=""), name="name_set")),
            """,
        )
        assert folds(self.project.compact()) == []

    def test_index_name_collision(self):
        self.project.migration("shop", "0001_initial", ITEM)
        # The old index can't be removed (its source isn't a literal), so its name
        # is taken until RemoveIndex: Item's index can't exist from 0001.
        self.project.migration(
            "lib",
            "0001_initial",
            source="""
            from django.db import migrations, models

            OPTIONS = {"indexes": [models.Index(fields=["x"], name="the_idx")]}


            class Migration(migrations.Migration):
                operations = [
                    migrations.CreateModel(
                        name="Other",
                        fields=[
                            ("id", models.AutoField(primary_key=True)),
                            ("x", models.IntegerField()),
                        ],
                        options=OPTIONS,
                    ),
                ]
            """,
        )
        self.project.migration(
            "lib",
            "0002_drop",
            'migrations.RemoveIndex("other", "the_idx"),',
            deps=[("lib", "0001_initial")],
        )
        self.project.migration(
            "shop",
            "0002_idx",
            'migrations.AddIndex("item", models.Index(fields=["name"], name="the_idx")),',
            extra_deps=[("lib", "0002_drop")],
        )
        result = self.project.check_equivalence()
        assert folds(result) == []

    def test_together(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_ut",
            'migrations.AlterUniqueTogether("item", {("id", "name")}),',
        )
        result = self.project.check_equivalence()
        assert folds(result) == [
            (
                "shop.Item",
                "AlterUniqueTogether in 0002_ut folded into CreateModel in 0001_initial",
            )
        ]
        assert "'unique_together': {('id', 'name')}," in self.project.read(
            "shop", "0001_initial"
        )

    def test_model_options_and_comment(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_opts",
            """
            migrations.AlterModelOptions("item", {"verbose_name": "thing", "ordering": ["name"]}),
            migrations.AlterModelTableComment("item", "Things"),
            """,
        )
        result = self.project.check_equivalence()
        assert result.folded_operations == 2
        source = self.project.read("shop", "0001_initial")
        assert "'verbose_name': 'thing'," in source
        assert "'db_table_comment': 'Things'," in source

    def test_default_managers_not_folded(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_managers",
            'migrations.AlterModelManagers("item", [("objects", models.Manager())]),',
        )
        # CreateModel can't express the default manager list explicitly.
        assert folds(self.project.check_equivalence()) == []

    def test_managers(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_managers",
            'migrations.AlterModelManagers("item", [("things", models.Manager())]),',
        )
        result = self.project.check_equivalence()
        assert result.folded_operations == 1

    def test_skip_kind_and_plan_file(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_qty",
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),',
        )
        out, _ = self.project.command("plan", "--static", "--skip-kind", "fold")
        assert out == "No operations to remove.\n"
        plan_path = self.tmp_path / "plan.toml"
        out, _ = self.project.command(
            "plan", "--static", "-v", "2", "-o", str(plan_path)
        )
        lines = out.splitlines()
        assert lines[2].split() == [
            "qty",
            "fold",
            "0002_qty",
            "->",
            "0001_initial",
            "1",
            "operation",
        ]
        assert lines[3].strip().endswith("0002_qty.py:11  AddField")
        assert (
            lines[4].strip().endswith("0001_initial.py:10  CreateModel (regenerated)")
        )
        assert lines[6] == (
            "Would fold 1 operation and edit 1 operation in 2 migration files."
            " 1 migration would be left with no operations."
        )
        data = tomllib.loads(plan_path.read_text())
        assert data["summary"]["folded_operations"] == 1
        assert data["removals"][0]["targets"][0]["detail"] == "regenerated"
        self.project.command("apply", "--plan", str(plan_path))
        assert "('qty', models.IntegerField(default=1))," in self.project.read(
            "shop", "0001_initial"
        )

    def test_blocked_reason_still_reported(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop",
            "0003_remove",
            """
            migrations.RunSQL("SELECT legacy FROM shop_item"),
            migrations.RemoveField("item", "legacy"),
            """,
        )
        result = self.project.compact()
        assert "shop.Item.legacy" in blocked(result)

    def test_rounds_see_original_to_fields(self):
        self.project.migration(
            "shop",
            "0001_initial",
            ITEM
            + """
            migrations.CreateModel(
                name="Tag",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("code", models.CharField(max_length=5, unique=True)),
                ],
            ),
            """,
        )
        self.project.migration(
            "shop",
            "0002_fk",
            'migrations.AddField("item", "tag", models.ForeignKey("shop.tag", models.CASCADE, to_field="code", null=True)),',
        )
        self.project.migration(
            "shop", "0003_rename", 'migrations.RenameField("tag", "code", "slug"),'
        )
        self.project.migration(
            "shop",
            "0004_qty",
            'migrations.AddField("tag", "qty", models.IntegerField(default=1)),',
        )
        self.project.migration(
            "shop",
            "0005_fk",
            """
            migrations.AlterField("item", "tag", models.ForeignKey("shop.tag", models.CASCADE, null=True)),
            migrations.RemoveField("tag", "slug"),
            """,
        )
        result = self.project.check_equivalence()
        assert "shop.Tag.slug" in blocked(result)
        assert result.folded_operations >= 1

    def test_names_must_be_stable_across_window(self):
        self.project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("name", models.CharField(max_length=10)),
                    ("slug", models.CharField(max_length=10)),
                ],
            ),
            """,
        )
        self.project.migration(
            "shop", "0002_rename", 'migrations.RenameField("item", "name", "title"),'
        )
        self.project.migration(
            "shop",
            "0003_more",
            """
            migrations.AlterUniqueTogether("item", {("title", "slug")}),
            migrations.AddIndex("item", models.Index(fields=["title"], name="title_idx")),
            migrations.AlterModelOptions("item", {"ordering": ["title"]}),
            migrations.AddIndex("item", models.Index(fields=["slug"], name="slug_idx")),
            """,
        )
        result = self.project.check_equivalence()
        # Each of these names "title", which was "name" when the model was
        # created; the slug index can't jump the queue of indexes either.
        assert folds(result) == []

    def test_duplicate_imports_not_added(self):
        self.project.migration(
            "shop",
            "0001_initial",
            source="""
            from django.db import migrations, models


            class Migration(migrations.Migration):
                operations = [
                    migrations.CreateModel(name="Tag", fields=[("id", models.AutoField(primary_key=True))]),
                    migrations.CreateModel(name="Item", fields=[("id", models.AutoField(primary_key=True))]),
                    migrations.CreateModel(name="Line", fields=[("id", models.AutoField(primary_key=True))]),
                ]
            """,
        )
        self.project.migration(
            "shop",
            "0002_fks",
            """
            migrations.AddField("item", "tag", models.ForeignKey("shop.tag", models.CASCADE)),
            migrations.AddField("line", "tag", models.ForeignKey("shop.tag", models.CASCADE)),
            """,
            deps=[("shop", "0001_initial")],
        )
        self.project.check_equivalence()
        assert (
            self.project.read("shop", "0001_initial").count(
                "import django.db.models.deletion"
            )
            == 1
        )

    def test_unchanged_operation_not_regenerated(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_qty",
            source="""
            from django.db import migrations, models


            class Migration(migrations.Migration):
                dependencies = [("shop", "0001_initial"), ("other", "0001_initial")]
                operations = [
                    migrations.AddField("item", "qty", models.IntegerField(default=1)),  # keep me
                ]
            """,
        )
        # A RunSQL mentioning the model keeps the AddField out of the CreateModel.
        self.project.migration(
            "other",
            "0001_initial",
            "migrations.RunSQL(\"SELECT 'item'\"),",
            deps=[("shop", "0001_initial")],
        )
        self.project.migration(
            "shop",
            "0003_qty",
            'migrations.AlterField("item", "qty", models.IntegerField(default=1)),',
            deps=[("shop", "0002_qty")],
        )
        result = self.project.check_equivalence()
        assert result.folded_operations == 1
        assert (
            'migrations.AddField("item", "qty", models.IntegerField(default=1)),  # keep me'
            in self.project.read("shop", "0002_qty")
        )

    def test_seeded_rows_keep_their_default(self):
        seeded(
            self.project,
            "models.IntegerField(default=0)",
            "models.IntegerField(default=5)",
        )
        result = self.project.compact()
        assert folds(result) == []
        # A trial runs it and sees the seeded row get 5 instead of 0.
        assert folds(self.project.compact(static=False)) == []

    def test_seeded_rows_same_default_folds(self):
        seeded(
            self.project,
            "models.IntegerField(default=0)",
            "models.IntegerField(null=True)",
        )
        assert folds(self.project.check_equivalence()) == [
            (
                "shop.Item.qty",
                "AlterField in 0004_alter folded into AddField in 0003_add",
            )
        ]

    def test_seeded_rows_trial(self):
        seeded(
            self.project,
            "models.IntegerField(null=True)",
            "models.IntegerField(null=True, default=5)",
        )
        assert folds(self.project.compact()) == []
        # The seeded row stays NULL; added with the default, it would get 5.
        assert folds(self.project.compact(static=False)) == []

    def test_rows_not_written_trial_unlocks(self):
        self.project.migration("shop", "0001_initial", ITEM)
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
            "shop",
            "0003_add",
            'migrations.AddField("item", "qty", models.IntegerField(default=0)),',
        )
        self.project.migration(
            "shop",
            "0004_alter",
            'migrations.AlterField("item", "qty", models.IntegerField(default=5)),',
        )
        assert folds(self.project.compact()) == []
        # Even into the CreateModel, across the data migration.
        assert folds(self.project.compact(static=False)) == [
            (
                "shop.Item.qty",
                "AddField in 0003_add folded into CreateModel in 0001_initial",
            ),
            (
                "shop.Item.qty",
                "AlterField in 0004_alter folded into CreateModel in 0001_initial",
            ),
        ]

    def test_mentions_unserializable(self):
        assert _mentions({"x": object()}, "name") is True
