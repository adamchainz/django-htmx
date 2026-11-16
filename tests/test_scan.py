"""The name scan of opaque operations, piece by piece."""

from __future__ import annotations

import ast
from typing import Any

from django_compact_migrations import history as history_module
from django_compact_migrations.core import analyse
from django_compact_migrations.opaque import (
    OpaqueIndex,
    Text,
    _metadata_statements,
    _migration_text,
)
from tests.project import ITEM, Project, blocked, kinds


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


def test_empty_name_never_mentioned():
    assert Text("anything", match_all=False).mentions("") is False


def test_metadata_statements():
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


def test_unreadable_source(project, monkeypatch):
    analysis = scanned(project)
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


def test_index_edges(project):
    analysis = scanned(project)
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


def test_block_policy_names_the_operation(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "legacy", models.IntegerField(default=0)),\n'
        'migrations.RunSQL("SELECT 1"),',
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )
    result = project.compact(opaque="block")
    assert blocked(result)["shop.Item.legacy"] == (
        "opaque RunSQL in shop.0002_add may run meanwhile",
    )


class TestReverseNames:
    def test_default_query_name(self, project):
        project.migration("shop", "0001_initial", ITEM + PERSON)
        project.migration(
            "shop",
            "0002_owner",
            'migrations.AddField("item", "owner", models.ForeignKey("shop.person",'
            " models.SET_NULL, null=True)),",
        )
        project.migration(
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
        project.migration(
            "shop", "0004_drop", 'migrations.RemoveField("item", "owner"),'
        )
        result = project.check_equivalence()
        assert result.removals == []
        assert blocked(result)["shop.Item.owner"] == (
            "RunPython in shop.0003_data mentions 'item'",
        )

    def test_default_related_name(self, project):
        project.migration("shop", "0001_initial", ITEM + PERSON)
        project.migration(
            "shop",
            "0002_owner",
            """
            migrations.AlterModelOptions("item", {"default_related_name": "things"}),
            migrations.AddField("item", "owner", models.ForeignKey("shop.person", models.SET_NULL, null=True)),
            """,
        )
        project.migration(
            "other",
            "0001_data",
            'migrations.RunSQL("SELECT 1 -- things"),',
            deps=[("shop", "0002_owner")],
        )
        project.migration(
            "shop",
            "0003_drop",
            'migrations.RemoveField("item", "owner"),',
            extra_deps=[("other", "0001_data")],
        )
        result = project.compact("shop.Item.owner")
        assert blocked(result)["shop.Item.owner"] == (
            "RunSQL in other.0001_data mentions 'things'",
        )


def test_dependency_names_not_scanned(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_item_legacy",
        'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
    )
    project.migration("shop", "0003_data", "migrations.RunSQL('SELECT 1'),")
    project.migration(
        "shop", "0004_remove", 'migrations.RemoveField("item", "legacy"),'
    )
    result = project.check_equivalence()
    assert kinds(result) == [("field", "shop.Item.legacy")]


PERSON = """
    migrations.CreateModel(
        name="Person",
        fields=[("id", models.AutoField(primary_key=True))],
    ),
"""
