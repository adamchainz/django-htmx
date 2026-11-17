"""The name scan of opaque operations, piece by piece."""

from __future__ import annotations

import ast
from typing import Any

from unittest_parametrize import parametrize

from django_compact_migrations import history as history_module
from django_compact_migrations.core import analyse
from django_compact_migrations.opaque import (
    OpaqueIndex,
    Text,
    _metadata_statements,
    _migration_text,
    _source_words,
    python_words,
)
from tests.project import (
    ITEM,
    Project,
    ProjectTestCase,
    blocked,
    kinds,
)


def scanned(project: Project) -> Any:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_data",
        'migrations.RunSQL("SELECT name FROM shop_item"),',
    )
    project.activate()
    with project.db_blocker.unblock():
        return analyse()


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


class ReverseNamesTests(ProjectTestCase):
    def test_default_query_name(self):
        self.project.migration("shop", "0001_initial", ITEM + PERSON)
        self.project.migration(
            "shop",
            "0002_owner",
            'migrations.AddField("item", "owner", models.ForeignKey("shop.person",'
            " models.SET_NULL, null=True)),",
        )
        self.project.migration(
            "shop",
            "0003_data",
            "migrations.RunPython(count),",
            extra="""
            def count(apps, schema_editor):
                Person = apps.get_model("shop", "Person")
                Person.objects.using(schema_editor.connection.alias).filter(
                    item__isnull=False
                ).count()
            """,
        )
        self.project.migration(
            "shop", "0004_drop", 'migrations.RemoveField("item", "owner"),'
        )
        result = self.project.check_equivalence()
        assert result.removals == []
        assert blocked(result)["shop.Item.owner"] == (
            "RunPython in shop.0003_data mentions 'item'",
        )

    def test_default_related_name(self):
        self.project.migration("shop", "0001_initial", ITEM + PERSON)
        self.project.migration(
            "shop",
            "0002_owner",
            """
            migrations.AlterModelOptions("item", {"default_related_name": "things"}),
            migrations.AddField("item", "owner", models.ForeignKey("shop.person", models.SET_NULL, null=True)),
            """,
        )
        self.project.migration(
            "other",
            "0001_data",
            'migrations.RunSQL("SELECT 1 -- things"),',
            deps=[("shop", "0002_owner")],
        )
        self.project.migration(
            "shop",
            "0003_drop",
            'migrations.RemoveField("item", "owner"),',
            extra_deps=[("other", "0001_data")],
        )
        result = self.project.compact("shop.Item.owner")
        assert blocked(result)["shop.Item.owner"] == (
            "RunSQL in other.0001_data mentions 'things'",
        )


PERSON = """
    migrations.CreateModel(
        name="Person",
        fields=[("id", models.AutoField(primary_key=True))],
    ),
"""


class OpaqueTests(ProjectTestCase):
    def test_empty_name_never_mentioned(self):
        assert Text("anything", match_all=False).mentions("") is False

    def test_metadata_statements(self):
        tree = ast.parse(
            "class Migration:\n"
            "    initial: bool = True\n"
            "    dependencies = []\n"
            "    dependencies += [('a', 'b')]\n"
            "    operations = []\n"
            "    def method(self):\n"
            "        pass\n"
        )
        assert [type(s).__name__ for s in _metadata_statements(tree)] == [
            "AnnAssign",
            "Assign",
            "AugAssign",
        ]

    def test_unreadable_source(self):
        analysis = scanned(self.project)
        info = analysis.history.migrations[1]
        without = history_module.MigrationInfo(
            info.key, info.migration, info.index, info.path, False, None
        )
        text, unknown = _migration_text(without)
        assert "shop_item" in text and not unknown
        assert info.path is not None
        without.path = info.path.with_name("missing.py")
        assert _migration_text(without) == ("", True)
        without.path = None
        assert _migration_text(without) == ("", True)

    def test_dependency_names_not_scanned(self):
        self.project.migration("shop", "0001_initial", ITEM)
        self.project.migration(
            "shop",
            "0002_item_legacy",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        self.project.migration("shop", "0003_data", "migrations.RunSQL('SELECT 1'),")
        self.project.migration(
            "shop", "0004_remove", 'migrations.RemoveField("item", "legacy"),'
        )
        result = self.project.check_equivalence()
        assert kinds(result) == [("field", "shop.Item.legacy")]

    def test_locals_parameters_and_comments_ignored(self):
        dead_field(
            self.project,
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
        assert kinds(self.project.check_equivalence()) == [("field", "shop.Item.event")]

    @parametrize(
        "body",
        [
            'apps.get_model("shop", "Item").objects.filter(event=1)',
            'apps.get_model("shop", "Item").objects.values_list("event")',
            'row = apps.get_model("shop", "Item").objects.first()\n    row.event',
            'print(f"{1}event_count")',
            'print(b"event")',
        ],
    )
    def test_names_still_found(self, body):
        dead_field(
            self.project,
            "event",
            "migrations.RunPython(forwards),",
            extra=f"def forwards(apps, schema_editor):\n    {body}\n",
        )
        assert blocked(self.project.compact())["shop.Item.event"] == (
            "RunPython in shop.0002_add mentions 'event'",
        )

    def test_imports_count(self):
        (self.project.root / self.project.package / "event.py").write_text("X = 1\n")
        dead_field(
            self.project,
            "event",
            "migrations.RunPython(migrations.RunPython.noop),",
            extra=f"from {self.project.package} import event as e",
        )
        assert blocked(self.project.compact())["shop.Item.event"] == (
            "RunPython in shop.0002_add mentions 'event'",
        )

    def test_custom_operation_parameters_ignored(self):
        (self.project.root / self.project.package / "ops.py").write_text(
            "from django.db import migrations\n\n\n"
            "class IgnoreMigration(migrations.operations.base.Operation):\n"
            "    def state_forwards(self, app_label, state):\n        pass\n\n"
            "    def database_forwards(self, app_label, schema_editor, from_state, to_state):\n"
            "        pass\n"
        )
        dead_field(
            self.project,
            "state",
            f"{self.project.package}.ops.IgnoreMigration(),",
            extra=f"import {self.project.package}.ops",
        )
        assert kinds(self.project.check_equivalence()) == [("field", "shop.Item.state")]

    def test_get_model_app_label_ignored(self):
        dead_field(
            self.project,
            "shop",
            "migrations.RunPython(forwards),",
            extra="""
            def forwards(apps, schema_editor):
                apps.get_model("shop", "Item")
            """,
        )
        assert kinds(self.project.compact()) == [("field", "shop.Item.shop")]
        assert "shop" in python_words(__import__("ast").parse('get_model("shop.Item")'))

    def test_fallback_to_raw_text(self):
        assert _source_words("lambda: event)") == "lambda: event)"

    def test_words(self):

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

    def test_index_edges(self):
        analysis = scanned(self.project)
        index = OpaqueIndex(analysis.history, analysis.timeline.opaque)
        # A name without words matches nothing (but match-all texts).
        assert index._candidates("--") == index.match_all
        assert index._outside((0, 0), None) == analysis.history.ancestors[0]
        (o,) = index.ops
        # An installed package's operation can only know about what was created
        # before it in the graph, or earlier in the same migration.
        o.rec.migration.editable = False
        assert index.contains(o, (0, 0), None, (o.pos[0], -1)) is True
        assert index.contains(o, (0, 0), None, (o.pos[0], 0)) is False
