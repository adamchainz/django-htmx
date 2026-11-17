"""Precision of the opaque scan: fewer false positives, same safety."""

from __future__ import annotations

import pytest

from django_compact_migrations.opaque import _source_words, python_words
from tests.project import ITEM, Project, blocked, kinds


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


class TestPythonWords:
    def test_locals_parameters_and_comments_ignored(self, project):
        dead_field(
            project,
            "event",
            "migrations.RunPython(forwards),",
            extra="""
            def forwards(apps, schema_editor, event=None):
                # Touches every event.
                items = apps.get_model("shop", "Item").objects
                for event in items.using(schema_editor.connection.alias).all():
                    print(event)
            """,
        )
        assert kinds(project.check_equivalence()) == [("field", "shop.Item.event")]

    @pytest.mark.parametrize(
        "body",
        [
            'apps.get_model("shop", "Item").objects.filter(event=1)',
            'apps.get_model("shop", "Item").objects.values_list("event")',
            'row = apps.get_model("shop", "Item").objects.first()\n    row.event',
            'print(f"{1}event_count")',
            'print(b"event")',
        ],
    )
    def test_names_still_found(self, project, body):
        dead_field(
            project,
            "event",
            "migrations.RunPython(forwards),",
            extra=f"def forwards(apps, schema_editor):\n    {body}\n",
        )
        assert blocked(project.compact())["shop.Item.event"] == (
            "RunPython in shop.0002_add mentions 'event'",
        )

    def test_imports_count(self, project):
        (project.root / project.package / "event.py").write_text("X = 1\n")
        dead_field(
            project,
            "event",
            "migrations.RunPython(migrations.RunPython.noop),",
            extra=f"from {project.package} import event as e",
        )
        assert blocked(project.compact())["shop.Item.event"] == (
            "RunPython in shop.0002_add mentions 'event'",
        )

    def test_custom_operation_parameters_ignored(self, project):
        (project.root / project.package / "ops.py").write_text(
            "from django.db import migrations\n\n\n"
            "class IgnoreMigration(migrations.operations.base.Operation):\n"
            "    def state_forwards(self, app_label, state):\n        pass\n\n"
            "    def database_forwards(self, app_label, schema_editor, from_state, to_state):\n"
            "        pass\n"
        )
        dead_field(
            project,
            "state",
            f"{project.package}.ops.IgnoreMigration(),",
            extra=f"import {project.package}.ops",
        )
        assert kinds(project.check_equivalence()) == [("field", "shop.Item.state")]

    def test_get_model_app_label_ignored(self, project):
        dead_field(
            project,
            "shop",
            "migrations.RunPython(forwards),",
            extra="""
            def forwards(apps, schema_editor):
                apps.get_model("shop", "Item")
            """,
        )
        assert kinds(project.compact()) == [("field", "shop.Item.shop")]
        assert "shop" in python_words(__import__("ast").parse('get_model("shop.Item")'))

    def test_fallback_to_raw_text(self):
        assert _source_words("lambda: event)") == "lambda: event)"

    def test_words(self):
        import ast

        tree = ast.parse(
            "import a.b as c\nfrom d.e import f\nx.g(h=1, *i)\ndef j(k): l\n'm'\n"
        )
        assert sorted(python_words(tree).split("\n")) == [
            "a.b",
            "c",
            "d.e",
            "f",
            "g",
            "h",
            "m",
        ]


class TestThirdParty:
    def setup(
        self,
        project: Project,
        monkeypatch: pytest.MonkeyPatch,
        deps: list[tuple[str, str]],
    ) -> None:
        from django_compact_migrations import history

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

    def test_unordered_ignored(self, project, monkeypatch):
        self.setup(project, monkeypatch, [("shop", "0001_initial")])
        assert kinds(project.compact()) == [("field", "shop.Item.legacy")]

    def test_descendant_counts(self, project, monkeypatch):
        self.setup(project, monkeypatch, [("shop", "0002_add")])
        assert blocked(project.compact())["shop.Item.legacy"] == (
            "RunSQL in vendor.0001_initial mentions 'legacy'",
        )


class TestPoisoningWidth:
    def test_sdas_index_poisons_only_index(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop",
            "0003_drop",
            """
            migrations.RemoveField("item", "legacy"),
            migrations.RemoveIndex("item", "n_idx"),
            migrations.DeleteModel("Item"),
            """,
        )
        result = project.compact()
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
        project.check_equivalence()

    def test_state_only_remove_field(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        project.migration(
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
        result = project.compact()
        assert blocked(result)["shop.Item.legacy"] == (
            "ended by SeparateDatabaseAndState in shop.0003_unmount",
        )
        assert "shop.Item" not in blocked(result)


def test_blocked_lines_name_their_migrations(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "body", models.IntegerField(default=0)),',
    )
    project.migration("shop", "0003_remove", 'migrations.RemoveField("item", "body"),')
    project.migration(
        "shop",
        "0004_add",
        """
        migrations.AddField("item", "body", models.IntegerField(default=0)),
        migrations.RunSQL("SELECT body FROM shop_item"),
        """,
    )
    project.migration("shop", "0005_remove", 'migrations.RemoveField("item", "body"),')
    # Without folds: the kept AddField would fold into the CreateModel, and
    # the blocked field be reported as added there.
    out, _ = project.command("plan", "--static", "-v", "2", "--skip-kind", "fold")
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


NETBOX_OPTIONS = """
migrations.AlterModelOptions("item", {"ordering": ORDERING}),
migrations.AddField("item", "_name", models.CharField(blank=True, default="", max_length=100)),
"""


class TestSoftReferences:
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

    def test_netbox_shape(self, project):
        self.history(
            project,
            "('_name', 'pk')",
            'list(apps.get_model("shop", "Item").objects.using(alias).all())',
        )
        result = project.check_equivalence(skip_kinds=["fold"])
        assert result.removals == []
        assert blocked(result)["shop.Item._name"] == (
            "AlterModelOptions in shop.0002_name orders by it, and meanwhile"
            " RunPython in shop.0003_data mentions 'item'",
        )

    @pytest.mark.parametrize(
        "ordering",
        ["['-_name']", "(models.F('_name').asc(),)", "['_name__lower']"],
    )
    def test_ordering_forms(self, project, ordering):
        self.history(project, ordering, 'apps.get_model("shop", "Item")')
        assert "shop.Item._name" in blocked(project.compact())

    def test_get_latest_by(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        assert "shop.Item.stamp" in blocked(project.compact())

    def test_period_outside_query(self, project):
        # The query runs after the ordering stopped naming the field.
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        assert kinds(project.check_equivalence("shop.Item._name")) == [
            ("field", "shop.Item._name")
        ]

    def test_query_through_many_to_many(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop",
            "0003_drop",
            """
            migrations.AlterModelOptions("item", {}),
            migrations.RemoveField("item", "_name"),
            """,
        )
        assert "shop.Item._name" in blocked(
            project.compact(skip_kinds=["fold", "superseded"])
        )

    def test_query_through_child_model(self, project):
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
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
        project.migration(
            "shop",
            "0003_drop",
            """
            migrations.AlterModelOptions("item", {}),
            migrations.RemoveField("item", "_name"),
            """,
        )
        assert "shop.Item._name" in blocked(
            project.compact(skip_kinds=["fold", "superseded"])
        )
