from __future__ import annotations

import pytest

from django_compact_migrations.core import CompactError, Result
from tests.project import Project

ITEM = """
    migrations.CreateModel(
        name="Item",
        fields=[
            ("id", models.AutoField(primary_key=True)),
            ("name", models.CharField(max_length=10)),
        ],
    ),
"""


def kinds(result: Result) -> list[tuple[str, str]]:
    return sorted((r.kind, r.label) for r in result.removals)


def blocked(result: Result) -> dict[str, tuple[str, ...]]:
    return {b.label: b.reasons for b in result.blocked}


class TestModel:
    def test_removed(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        result = project.check_equivalence()
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
        assert "operations = []" in project.read("shop", "0003_drop")

    def test_renamed_model(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration("shop", "0003_drop", 'migrations.DeleteModel("New"),')
        result = project.check_equivalence()
        assert kinds(result) == [("model", "shop.New")]
        assert len(result.removals[0].elements) == 3

    def test_name_reuse(self, project):
        project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Thing",
                fields=[("id", models.AutoField(primary_key=True))],
            ),
            """,
        )
        project.migration("shop", "0002_drop", 'migrations.DeleteModel("Thing"),')
        project.migration(
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
        result = project.check_equivalence()
        assert [r.description for r in result.removals] == [
            "model created in 0001_initial, deleted in 0002_drop"
        ]
        assert "size" in project.read("shop", "0003_again")

    def test_live_model_untouched(self, project):
        project.migration("shop", "0001_initial", ITEM)
        result = project.compact()
        assert result.removals == []
        assert not result.changed


class TestField:
    def test_add_remove(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.AlterField("item", "legacy", models.IntegerField(null=True)),
            migrations.RenameField("item", "legacy", "old"),
            """,
        )
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "old"),'
        )
        result = project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.old")]
        assert result.removals[0].description == (
            "field added in 0002_add, removed in 0003_remove"
        )
        assert result.removed_operations == 4

    def test_create_model_entry(self, project):
        project.migration(
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
        project.migration(
            "shop", "0002_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.legacy")]
        assert result.removed_operations == 1
        assert result.edited_operations == 1
        assert project.read("shop", "0001_initial").endswith(
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

    def test_primary_key_blocked(self, project):
        project.migration(
            "shop",
            "0001_initial",
            """
            migrations.CreateModel(
                name="Item",
                fields=[("code", models.CharField(max_length=5, primary_key=True))],
            ),
            """,
        )
        project.migration(
            "shop",
            "0002_swap",
            """
            migrations.AddField("item", "id", models.IntegerField(default=0)),
            migrations.RemoveField("item", "code"),
            """,
        )
        result = project.compact()
        assert result.removals == []
        assert blocked(result) == {"shop.Item.code": ("it is or was a primary key",)}

    def test_referenced_by_live_index(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.AddIndex("item", models.Index(fields=["legacy"], name="legacy_idx")),
            """,
        )
        # A RemoveField that leaves a stale index in state (inconsistent but
        # accepted by Django's state) must not be removed.
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = project.compact()
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "referenced by AddIndex in shop.0002_add",
        )

    def test_generated_field_reference(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop",
            "0003_remove",
            """
            migrations.RemoveField("item", "double"),
            migrations.RemoveField("item", "price"),
            """,
        )
        result = project.check_equivalence()
        assert kinds(result) == [
            ("field", "shop.Item.double"),
            ("field", "shop.Item.price"),
        ]

    def test_generated_field_blocks(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "price"),'
        )
        result = project.compact("shop.Item.price")
        assert result.removals == []
        assert blocked(result)["shop.Item.price"] == (
            "referenced by AddField in shop.0002_add",
        )


class TestIndexAndConstraint:
    def test_index(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_index",
            """
            migrations.AddIndex("item", models.Index(fields=["name"], name="name_idx")),
            migrations.RenameIndex("item", new_name="name_idx2", old_name="name_idx"),
            """,
        )
        project.migration(
            "shop", "0003_drop", 'migrations.RemoveIndex("item", "name_idx2"),'
        )
        result = project.check_equivalence()
        assert kinds(result) == [("index", "shop.Item")]
        assert result.removals[0].description == (
            "index 'name_idx' added in 0002_index, removed in 0003_drop"
        )
        assert result.removed_operations == 3

    def test_create_model_index_entry(self, project):
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
                options={
                    "indexes": [models.Index(fields=["name"], name="a_idx"), models.Index(fields=["id", "name"], name="b_idx")],
                },
            ),
            """,
        )
        project.migration(
            "shop", "0002_drop", 'migrations.RemoveIndex("item", "a_idx"),'
        )
        result = project.check_equivalence()
        assert kinds(result) == [("index", "shop.Item")]
        assert (
            '"indexes": [models.Index(fields=["id", "name"], name="b_idx")],'
            in project.read("shop", "0001_initial")
        )

    def test_constraint(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop", "0003_drop", 'migrations.RemoveConstraint("item", "name_set"),'
        )
        result = project.check_equivalence()
        assert kinds(result) == [("constraint", "shop.Item")]
        assert result.removed_operations == 3

    def test_create_model_constraint_entry(self, project):
        project.migration(
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
        project.migration(
            "shop", "0002_drop", 'migrations.RemoveConstraint("item", "qty_positive"),'
        )
        result = project.check_equivalence()
        assert kinds(result) == [("constraint", "shop.Item")]
        assert "constraints" not in project.read("shop", "0001_initial")
        assert "options={}," in project.read("shop", "0001_initial")

    def test_constraint_keeps_field_alive(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop",
            "0003_drop",
            """
            migrations.RemoveConstraint("item", "qty_positive"),
            migrations.RemoveField("item", "qty"),
            """,
        )
        # Selecting only the field pulls in the constraint that references it.
        result = project.check_equivalence("shop.Item.qty")
        assert sorted(kinds(result)) == [
            ("constraint", "shop.Item"),
            ("field", "shop.Item.qty"),
        ]

    def test_kind_selection_pulls_in(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop",
            "0003_drop",
            """
            migrations.RemoveConstraint("item", "qty_positive"),
            migrations.RemoveField("item", "qty"),
            """,
        )
        # The constraint is not a selected kind but is required by the field.
        result = project.compact("shop.Item.qty", kinds=["field"])
        assert sorted(kinds(result)) == [
            ("constraint", "shop.Item"),
            ("field", "shop.Item.qty"),
        ]
        # Only constraints selected: the field stays, the constraint goes.
        result = project.compact(kinds=["constraint"])
        assert kinds(result) == [("constraint", "shop.Item")]


class TestTogether:
    def test_alter_together_pair(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_unique",
            'migrations.AlterUniqueTogether("item", {("id", "name")}),',
        )
        project.migration(
            "shop", "0003_not_unique", 'migrations.AlterUniqueTogether("item", set()),'
        )
        result = project.check_equivalence()
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

    def test_entry_stripped(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_unique",
            'migrations.AlterUniqueTogether("item", {("id", "name"), ("name", "id")}),',
        )
        project.migration(
            "shop",
            "0003_less",
            'migrations.AlterUniqueTogether("item", {("name", "id")}),',
        )
        result = project.check_equivalence()
        assert '{("name", "id")}' in project.read("shop", "0002_unique")
        assert result.removed_operations == 1
        assert result.edited_operations == 1

    def test_create_model_option_and_rename_field(self, project):
        project.migration(
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
        project.migration(
            "shop", "0002_rename", 'migrations.RenameField("item", "a", "c"),'
        )
        project.migration(
            "shop", "0003_drop", 'migrations.AlterUniqueTogether("item", set()),'
        )
        result = project.check_equivalence()
        assert result.removed_operations == 1
        assert "unique_together" not in project.read("shop", "0001_initial")
        assert "options={}," in project.read("shop", "0001_initial")

    def test_rename_index_old_fields_chain(self, project):
        project.migration(
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
        project.migration(
            "shop",
            "0002_rename",
            'migrations.RenameIndex("item", new_name="a_b_idx", old_fields=("a", "b")),',
        )
        project.migration(
            "shop", "0003_drop", 'migrations.RemoveIndex("item", "a_b_idx"),'
        )
        result = project.check_equivalence()
        assert kinds(result) == [("together", "shop.Item")]
        assert result.removed_operations == 2
        assert result.edited_operations == 1
        assert "index_together" not in project.read("shop", "0001_initial")

    def test_together_keeps_field(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "code", models.IntegerField(default=0)),
            migrations.AlterUniqueTogether("item", {("name", "code")}),
            """,
        )
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "code"),'
        )
        result = project.compact()
        assert result.removals == []
        assert blocked(result)["shop.Item.code"][0].startswith(
            "referenced by AlterUniqueTogether"
        )


def author_book(project: Project, book_ops: str = "", app: str = "library") -> None:
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


class TestRelations:
    def test_cross_app_fk_pulled_in(self, project):
        author_book(project)
        project.migration(
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
        project.migration(
            "shop", "0002_drop_author", 'migrations.RemoveField("book", "author"),'
        )
        project.migration(
            "library",
            "0002_drop",
            'migrations.DeleteModel("Author"),',
            extra_deps=[("shop", "0002_drop_author")],
        )
        result = project.check_equivalence("library")
        assert kinds(result) == [
            ("field", "shop.Book.author"),
            ("model", "library.Author"),
        ]

    def test_cross_app_fk_excluded(self, project):
        author_book(project)
        project.migration(
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
        project.migration(
            "shop", "0002_drop_author", 'migrations.RemoveField("book", "author"),'
        )
        project.migration(
            "library",
            "0002_drop",
            'migrations.DeleteModel("Author"),',
            extra_deps=[("shop", "0002_drop_author")],
        )
        result = project.compact("library", exclude=["shop"])
        assert result.removals == []
        assert blocked(result) == {
            "library.Author": (
                "referenced by CreateModel in shop.0001_initial (field 'author')",
            )
        }

    def test_live_fk_blocks(self, project):
        author_book(project)
        project.migration(
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
        project.migration(
            "library",
            "0002_drop",
            # Inconsistent but accepted by the state: a dangling FK remains.
            'migrations.DeleteModel("Author"),',
            extra_deps=[("shop", "0001_initial")],
        )
        result = project.compact()
        assert result.removals == []
        assert blocked(result)["library.Author"] == (
            "referenced by AddField in shop.0001_initial",
        )

    def test_to_field(self, project):
        project.migration(
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
        project.migration(
            "shop",
            "0002_drop",
            """
            migrations.AlterField("line", "item", models.ForeignKey("shop.item", models.CASCADE)),
            migrations.RemoveField("item", "code"),
            """,
        )
        result = project.compact("shop.Item.code")
        assert result.removals == []
        assert blocked(result)["shop.Item.code"] == (
            "referenced by CreateModel in shop.0001_initial (field 'item')",
        )

    def test_m2m_auto_through(self, project):
        author_book(project)
        project.migration(
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
        project.migration(
            "shop", "0002_drop", 'migrations.RemoveField("book", "authors"),'
        )
        result = project.check_equivalence()
        assert kinds(result) == [("field", "shop.Book.authors")]

    def test_m2m_through(self, project):
        author_book(project)
        project.migration(
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
        project.migration(
            "shop",
            "0002_drop",
            """
            migrations.RemoveField("book", "authors"),
            migrations.DeleteModel("Credit"),
            """,
        )
        result = project.check_equivalence("shop.Credit")
        assert kinds(result) == [
            ("field", "shop.Book.authors"),
            ("model", "shop.Credit"),
        ]

    def test_m2m_through_blocks(self, project):
        author_book(project)
        project.migration(
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
        project.migration(
            "shop",
            "0002_swap",
            """
            migrations.AlterField("book", "authors", models.ManyToManyField("library.author")),
            migrations.DeleteModel("Credit"),
            """,
        )
        result = project.compact()
        assert result.removals == []
        assert blocked(result)["shop.Credit"] == (
            "referenced by AddField in shop.0001_initial",
        )

    def test_base_model(self, project):
        project.migration(
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
        project.migration(
            "shop",
            "0002_drop",
            """
            migrations.DeleteModel("Child"),
            migrations.DeleteModel("Base"),
            """,
        )
        result = project.check_equivalence("shop.Base")
        assert kinds(result) == [("model", "shop.Base"), ("model", "shop.Child")]

    def test_base_model_blocks(self, project):
        project.migration(
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
        project.migration("shop", "0002_drop", 'migrations.DeleteModel("Base"),')
        result = project.compact()
        assert blocked(result)["shop.Base"] == (
            "referenced by CreateModel in shop.0001_initial",
        )

    def test_self_referencing(self, project):
        project.migration(
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
        project.migration("shop", "0002_drop", 'migrations.DeleteModel("Node"),')
        result = project.check_equivalence()
        assert kinds(result) == [("model", "shop.Node")]

    def test_later_model_with_same_name_referenced(self, project):
        # A FK created while its target name doesn't exist yet references the
        # life that later takes that name. (Not migratable, so state only.)
        project.migration(
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
        project.migration(
            "shop",
            "0002_drop",
            """
            migrations.RemoveField("line", "item"),
            migrations.DeleteModel("Item"),
            """,
        )
        result = project.compact("shop.Item", kinds=["model"])
        assert kinds(result) == [
            ("field", "shop.Line.item"),
            ("model", "shop.Item"),
        ]
        project.migration(
            "shop",
            "0002_drop",
            'migrations.DeleteModel("Item"),',
            deps=[("shop", "0001_initial")],
        )
        result = project.compact("shop.Item")
        assert blocked(result)["shop.Item"] == (
            "referenced by CreateModel in shop.0001_initial (field 'item')",
        )


class TestSuperseded:
    def test_alter_field_chain(self, project):
        project.migration("shop", "0001_initial", ITEM)
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
        project.migration(
            "shop",
            "0004_c",
            'migrations.AlterField("item", "name", models.CharField(max_length=40, db_index=True)),',
        )
        result = project.check_equivalence()
        assert [(r.kind, r.description) for r in result.removals] == [
            ("superseded", "AlterField in 0002_a superseded by AlterField in 0003_b"),
            ("superseded", "AlterField in 0003_b superseded by AlterField in 0004_c"),
        ]
        assert "max_length=40" in project.read("shop", "0004_c")

    def test_alter_field_type_guard(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.TextField()),',
        )
        project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "name", models.IntegerField(null=True)),',
        )
        result = project.compact()
        assert result.removals == []

    def test_alter_field_type_guard_one_side(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.CharField(max_length=20)),',
        )
        project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "name", models.TextField()),',
        )
        result = project.check_equivalence()
        assert [r.kind for r in result.removals] == ["superseded"]

    def test_alter_field_before_remove(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.TextField()),',
        )
        project.migration(
            "shop",
            "0003_index",
            'migrations.AddIndex("item", models.Index(fields=["name"], name="n_idx")),',
        )
        project.migration("shop", "0004_b", 'migrations.RemoveField("item", "name"),')
        # The index keeps the (dead) field; the AlterField is still superseded
        # by RemoveField but the index refers to the field meanwhile.
        result = project.compact()
        assert result.removals == []
        assert "shop.Item.name" in blocked(result)

    def test_alter_field_rename_between(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            """
            migrations.AlterField("item", "name", models.CharField(max_length=20)),
            migrations.RenameField("item", "name", "title"),
            migrations.AlterField("item", "title", models.CharField(max_length=30)),
            """,
        )
        assert project.compact().removals == []

    def test_alter_model_options(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            'migrations.AlterModelOptions("item", {"verbose_name": "thing"}),',
        )
        project.migration(
            "shop",
            "0003_b",
            'migrations.AlterModelOptions("item", {"verbose_name": "item"}),',
        )
        result = project.check_equivalence()
        assert [r.description for r in result.removals] == [
            "AlterModelOptions in 0002_a superseded by AlterModelOptions in 0003_b"
        ]

    def test_alter_model_options_managed(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            'migrations.AlterModelOptions("item", {"managed": False}),',
        )
        project.migration(
            "shop",
            "0003_b",
            'migrations.AlterModelOptions("item", {}),',
        )
        assert project.compact().removals == []

    def test_alter_model_managers(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            'migrations.AlterModelManagers("item", [("things", models.Manager())]),',
        )
        project.migration(
            "shop",
            "0003_b",
            'migrations.AlterModelManagers("item", [("objects", models.Manager())]),',
        )
        result = project.check_equivalence()
        assert [r.kind for r in result.removals] == ["superseded"]

    def test_alter_model_table_never_superseded(self, project):
        # Dropping 0002 would keep "shop_item" taken while Thing claims it.
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop", "0002_a", 'migrations.AlterModelTable("item", "old_item"),'
        )
        project.migration(
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
        project.migration(
            "shop", "0004_b", 'migrations.AlterModelTable("item", "archive_item"),'
        )
        result = project.check_equivalence()
        assert result.removals == []

    def test_alter_model_table_comment(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop", "0002_a", 'migrations.AlterModelTableComment("item", "a"),'
        )
        project.migration(
            "shop", "0003_b", 'migrations.AlterModelTableComment("item", "b"),'
        )
        result = project.check_equivalence()
        assert [r.kind for r in result.removals] == ["superseded"]

    def test_alter_model_table_with_element_between(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            """
            migrations.AlterModelTableComment("item", "a"),
            migrations.AddField("item", "qty", models.IntegerField(default=0)),
            migrations.AlterModelTableComment("item", "b"),
            """,
        )
        assert project.compact().removals == []

    def test_kind_filter(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop", "0002_a", 'migrations.AlterModelTableComment("item", "a"),'
        )
        project.migration(
            "shop", "0003_b", 'migrations.AlterModelTableComment("item", "b"),'
        )
        assert project.compact(kinds=["model", "field"]).removals == []
        assert len(project.compact(kinds=["superseded"]).removals) == 1
        assert len(project.compact("shop.Item").removals) == 1
        assert project.compact("shop.Item.name").removals == []
        assert project.compact(exclude=["shop.item"]).removals == []


class TestOpaque:
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

    def test_scan_hit(self, project):
        self._history(project, "migrations.RunPython(forwards),\n")
        result = project.check_equivalence()
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "RunPython in shop.0002_add mentions 'legacy'",
        )

    def test_scan_ignore(self, project):
        self._history(project, "migrations.RunPython(forwards),\n")
        result = project.compact(opaque="ignore")
        assert kinds(result) == [("field", "shop.Item.legacy")]

    def test_scan_miss(self, project):
        self._history(project, "migrations.RunSQL('SELECT 1'),\n")
        project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.RunSQL("UPDATE shop_item SET name = 'x'"),
            """,
            deps=[("shop", "0001_initial")],
        )
        result = project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.legacy")]
        result_block = project.compact(opaque="block")
        assert result_block.removals == []

    def test_window(self, project):
        project.migration(
            "shop",
            "0001_initial",
            ITEM + "migrations.RunSQL(\"SELECT 'legacy'\"),",
        )
        project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        project.migration(
            "shop",
            "0003_remove",
            """
            migrations.RemoveField("item", "legacy"),
            migrations.RunSQL("SELECT 'legacy'"),
            """,
        )
        result = project.check_equivalence(opaque="block")
        assert kinds(result) == [("field", "shop.Item.legacy")]

    def test_external_function(self, project):
        project.migration("shop", "0001_initial", ITEM)
        (project.root / project.package / "helpers.py").write_text(
            "def touch(apps, schema_editor):\n    apps.get_model('shop', 'Item').objects.filter(legacy=1)\n"
        )
        project.migration(
            "shop",
            "0002_add",
            f"""
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.RunPython({project.package}.helpers.touch),
            """,
            extra=f"import {project.package}.helpers",
        )
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = project.compact()
        assert blocked(result)["shop.Item.legacy"] == (
            "RunPython in shop.0002_add mentions 'legacy'",
        )

    def test_unknown_source_matches_everything(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.RunPython(print),
            """,
        )
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = project.compact()
        assert "mentions 'legacy'" in blocked(result)["shop.Item.legacy"][0]

    def test_noop_and_partial(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.RunPython(functools.partial(migrations.RunPython.noop), migrations.RunPython.noop),
            """,
            extra="import functools",
        )
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.legacy")]

    def test_soft_reference(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop",
            "0003_remove",
            """
            migrations.AlterModelOptions("item", {}),
            migrations.RemoveField("item", "rank"),
            """,
        )
        result = project.compact("shop.Item.rank")
        assert result.removals == []
        assert "orders by it" in blocked(result)["shop.Item.rank"][0]
        # Without the data migration the soft reference doesn't block.
        project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "rank", models.IntegerField(default=0)),
            migrations.AlterModelOptions("item", {"ordering": ["-rank"]}),
            """,
            deps=[("shop", "0001_initial")],
        )
        result = project.check_equivalence("shop.Item.rank")
        assert kinds(result) == [("field", "shop.Item.rank")]


class TestElidable:
    def test_removed_with_entity(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        result = project.check_equivalence()
        assert kinds(result) == [("elidable", "shop"), ("model", "shop.Coupon")]
        assert result.removed_operations == 3

    def test_unrelated_elidable_kept(self, project):
        project.migration(
            "shop",
            "0001_initial",
            ITEM + "migrations.RunSQL('SELECT 1', elidable=True),",
        )
        assert not project.compact().changed
        result = project.check_equivalence(elidable=True)
        assert kinds(result) == [("elidable", "shop")]

    def test_elidable_not_removable_blocks(self, project, monkeypatch):
        project.migration(
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
        project.migration(
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
        project.migration(
            "shop",
            "0003_drop",
            'migrations.DeleteModel("Coupon"),',
            deps=[("shop", "0002_sql")],
        )
        result = project.compact()
        assert result.removals == []
        (reason,) = blocked(result)["shop.Coupon"]
        assert reason == (
            "elidable RunSQL in shop.0002_sql mentions 'coupon' but cannot be"
            " removed: operations list in shop.0002_sql could not be parsed"
            " (operations is not a list or tuple literal)"
        )

    def test_elidable_with_state_change_is_opaque(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = project.compact(elidable=True)
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "created by RunSQL in shop.0002_sql",
        )


class TestPoisoning:
    def test_separate_database_and_state(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop",
            "0003_drop",
            """
            migrations.DeleteModel("Ghost"),
            migrations.RemoveField("item", "legacy"),
            """,
        )
        result = project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.legacy")]
        assert blocked(project.compact())["shop.Ghost"] == (
            "created by SeparateDatabaseAndState in shop.0002_sdas",
        )

    def test_sdas_touches_existing(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        project.migration(
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
        project.migration(
            "shop", "0004_drop", 'migrations.RemoveField("item", "legacy"),'
        )
        result = project.compact()
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "changed by SeparateDatabaseAndState in shop.0003_sdas",
        )

    def test_custom_operation(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop", "0003_drop", 'migrations.RemoveField("item", "legacy"),'
        )
        result = project.check_equivalence()
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "created by MyAddField in shop.0002_add",
        )

    def test_custom_operation_renaming_model(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_rename",
            'MyRename("Item", "Thing"),',
            extra="""
            class MyRename(migrations.RenameModel):
                pass
            """,
        )
        project.migration("shop", "0003_drop", 'migrations.DeleteModel("Thing"),')
        result = project.compact()
        assert result.removals == []
        assert blocked(result) == {
            "shop.Item": ("changed by MyRename in shop.0002_rename",),
            "shop.Thing": ("created by MyRename in shop.0002_rename",),
        }


class TestEditability:
    def test_unlocatable_operations(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop",
            "0003_drop",
            'migrations.RemoveField("item", "legacy"),',
            deps=[("shop", "0002_add")],
        )
        result = project.compact()
        assert result.removals == []
        assert blocked(result)["shop.Item.legacy"] == (
            "operations list in shop.0002_add could not be parsed (cannot match AddField in source)",
        )

    def test_unlocatable_sub_element(self, project):
        project.migration(
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
        project.migration(
            "shop",
            "0002_drop",
            'migrations.RemoveField("item", "legacy"),',
            deps=[("shop", "0001_initial")],
        )
        result = project.compact()
        assert blocked(result)["shop.Item.legacy"] == (
            "fields is not a literal in CreateModel in shop.0001_initial",
        )

    def test_non_editable(self, project, monkeypatch):
        from django_compact_migrations import history

        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        monkeypatch.setattr(
            history, "is_editable_path", lambda path: path.name != "0002_add.py"
        )
        result = project.compact()
        assert blocked(result)["shop.Item.legacy"] == (
            "shop.0002_add is not editable (installed package)",
        )


class TestOptions:
    def test_unknown_app(self, project):
        project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="No installed app with label 'nope'"):
            project.compact("nope")

    def test_bad_target(self, project):
        project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="Invalid target"):
            project.compact("shop.a.b.c")

    def test_bad_kind_and_policy(self, project):
        project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CompactError, match="Unknown kind"):
            project.compact(kinds=["nope"])
        with pytest.raises(CompactError, match="Unknown opaque policy"):
            project.compact(opaque="nope")

    def test_target_matches_any_name(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            """
            migrations.AddField("item", "legacy", models.IntegerField(default=0)),
            migrations.RenameField("item", "legacy", "older"),
            migrations.RenameModel("Item", "Product"),
            """,
        )
        project.migration(
            "shop", "0003_drop", 'migrations.RemoveField("product", "older"),'
        )
        assert len(project.compact("SHOP.item.LEGACY").removals) == 1
        assert len(project.compact("shop.Product.older").removals) == 1
        assert project.compact("shop.Product.name").removals == []
        assert project.compact("shop", exclude=["shop.Item.legacy"]).removals == []

    def test_replaced_migrations_warning(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            'migrations.AddField("item", "x", models.IntegerField(default=0)),',
        )
        project.migration(
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
        result = project.compact()
        assert result.warnings == [
            "2 migration(s) replaced by squashed migrations were skipped:"
            " shop.0001_initial, shop.0002_a"
        ]

    def test_broken_history(self, project):
        project.migration("shop", "0001_initial", 'migrations.DeleteModel("Nope"),')
        with pytest.raises(CompactError, match="refers to the model shop.Nope"):
            project.compact()


class TestMore:
    def test_unordered_branch_in_window(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        # Another app, unordered relative to shop.0002/0003, touches the column.
        project.migration(
            "other",
            "0001_initial",
            'migrations.RunSQL("UPDATE shop_item SET legacy = 1"),',
            deps=[("shop", "0001_initial")],
        )
        result = project.compact()
        assert blocked(result)["shop.Item.legacy"] == (
            "RunSQL in other.0001_initial mentions 'legacy'",
        )
        # Once ordered after the removal, it is outside the window.
        project.migration(
            "other",
            "0001_initial",
            "migrations.RunSQL(\"SELECT 'legacy'\"),",
            deps=[("shop", "0003_remove")],
        )
        assert kinds(project.check_equivalence()) == [("field", "shop.Item.legacy")]

    def test_postgres_operations_by_name(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop", "0003_drop", 'migrations.RemoveIndex("item", "n_idx"),'
        )
        result = project.check_equivalence()
        assert kinds(result) == [("index", "shop.Item")]
        assert "CreateExtension" in project.read("shop", "0002_index")

    def test_order_with_respect_to(self, project):
        project.migration(
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
        project.migration(
            "shop",
            "0002_drop",
            """
            migrations.AlterOrderWithRespectTo("line", None),
            migrations.RemoveField("line", "item"),
            """,
        )
        result = project.compact("shop.Line.item")
        assert blocked(result)["shop.Line.item"] == (
            "referenced by AlterOrderWithRespectTo in shop.0001_initial",
        )

    def test_unknown_index_poisons(self, project):
        project.migration(
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
        project.migration("shop", "0002_drop", 'migrations.DeleteModel("Thing"),')
        result = project.compact()
        assert blocked(result)["shop.Thing"] == (
            "unknown index 'nope' in RemoveIndex in shop.0001_initial",
        )

    def test_together_with_dead_field(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            """
            migrations.AddField("item", "x", models.IntegerField(default=0)),
            migrations.AlterUniqueTogether("item", {("name", "x")}),
            """,
        )
        project.migration(
            "shop",
            "0003_b",
            """
            migrations.AlterUniqueTogether("item", set()),
            migrations.RemoveField("item", "x"),
            """,
        )
        result = project.check_equivalence()
        assert ("field", "shop.Item.x") in kinds(result)
        assert result.empty_migrations == [("shop", "0002_a"), ("shop", "0003_b")]

    def test_together_flat_value(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            """
            migrations.AlterUniqueTogether("item", ("id", "name")),
            migrations.AlterModelOptions("item", {"verbose_name": "x"}),
            """,
        )
        project.migration(
            "shop", "0003_b", 'migrations.AlterUniqueTogether("item", None),'
        )
        result = project.check_equivalence()
        assert result.removed_operations == 2

    def test_constraint_only_target(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            """
            migrations.AddConstraint(
                "item",
                models.UniqueConstraint(fields=["name"], name="uniq_name"),
            ),
            """,
        )
        project.migration(
            "shop", "0003_b", 'migrations.RemoveConstraint("item", "uniq_name"),'
        )
        # A field target doesn't select constraints.
        assert project.compact("shop.Item.name").removals == []
        assert kinds(project.check_equivalence("shop.Item")) == [
            ("constraint", "shop.Item")
        ]

    def test_redundant_together_unordered_kept(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            """
            migrations.AddField("item", "x", models.IntegerField(default=0)),
            migrations.AlterUniqueTogether("item", {("name", "x")}),
            """,
        )
        project.migration(
            "shop", "0003_b", 'migrations.AlterUniqueTogether("item", set()),'
        )
        project.migration(
            "shop",
            "0002_c",
            'migrations.AlterUniqueTogether("item", {("id", "name")}),',
            deps=[("shop", "0001_initial")],
        )
        project.migration(
            "shop",
            "0004_merge",
            'migrations.RemoveField("item", "x"),',
            deps=[("shop", "0003_b"), ("shop", "0002_c")],
        )
        result = project.check_equivalence()
        assert ("field", "shop.Item.x") in kinds(result)
        # 0003_b is unordered relative to 0002_c's together change: kept.
        assert "AlterUniqueTogether" in project.read("shop", "0003_b")
        assert result.empty_migrations == [("shop", "0004_merge")]

    def test_dead_model_with_unlocatable_fields(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop",
            "0003_drop",
            'migrations.DeleteModel("Coupon"),',
            deps=[("shop", "0002_coupon")],
        )
        result = project.check_equivalence()
        assert kinds(result) == [("model", "shop.Coupon")]


class TestCustomStateChanges:
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

    def test_custom_create_model(self, project):
        project.migration(
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
        project.migration(
            "shop",
            "0002_drop",
            """
            migrations.RemoveIndex("thing", "a_idx"),
            migrations.RemoveConstraint("thing", "a_pos"),
            migrations.AlterUniqueTogether("thing", set()),
            migrations.RemoveField("thing", "b"),
            """,
        )
        result = project.check_equivalence()
        assert result.removals == []
        reasons = {r for b in result.blocked for r in b.reasons}
        assert reasons == {"created by MyCreateModel in shop.0001_initial"}
        assert len(result.blocked) == 4

    def test_custom_index_and_together(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop",
            "0003_drop",
            """
            migrations.RemoveIndex("item", "code_idx"),
            migrations.AlterUniqueTogether("item", set()),
            migrations.RemoveField("item", "code"),
            """,
        )
        result = project.check_equivalence()
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
