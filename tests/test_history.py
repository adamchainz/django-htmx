"""Reading migration files: where their operations are, and when they can't
be found."""

from __future__ import annotations

import ast
import importlib.util
import types
from pathlib import Path
from typing import Any

import pytest

from django_compact_migrations import history as history_module
from django_compact_migrations.history import (
    SourceFile,
    _read_source,
    callee_name,
    invalidate_modules,
    load_history,
    migration_path,
)
from tests.project import ITEM, Project, blocked, kinds, migration_source

ADD = 'migrations.AddField("item", "legacy", models.IntegerField(default=0))'


def odd_history(project: Project, body: str, extra: str = "") -> Any:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        source="import django.db.models.deletion\n"
        "from django.db import migrations, models\n"
        + extra
        + "\n\nclass Migration(migrations.Migration):\n"
        "    dependencies = [('shop', '0001_initial')]\n" + body,
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )
    return project.compact()


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (f"    operations = ops = [{ADD}]\n", "chained assignment to operations"),
        (
            f"    operations: list\n    operations = [{ADD}]\n",
            "annotation without value",
        ),
        (f"    operations = [{ADD}]\n    operations += []\n", "augmented assignment"),
        (f"    operations = [*[], {ADD}]\n", "starred expressions"),
        (
            f"    operations = []\n    operations.append({ADD})\n",
            "count does not match",
        ),
        # A tuple without brackets: nothing to edit the list with.
        (f"    operations = {ADD},\n", "unexpected token layout"),
        (f"    operations = []\n    operations = [{ADD}]\n", "no unique operations"),
    ],
)
def test_unlocatable(project, body, reason):
    result = odd_history(project, body)
    (message,) = blocked(result)["shop.Item.legacy"]
    assert message.startswith("operations list in shop.0002_add could not be parsed")
    assert reason in message


def test_two_migration_classes(project):
    body = f"    operations = [{ADD}]\n\n\nclass Migration(Migration):\n    pass\n"
    result = odd_history(project, body)
    assert (
        "no unique top-level class Migration" in blocked(result)["shop.Item.legacy"][0]
    )


def test_annotated_operations(project):
    result = odd_history(project, f"    operations: list = [{ADD}]\n")
    assert ("field", "shop.Item.legacy") in kinds(result)


def test_unreadable_source(project, monkeypatch):
    real = history_module._read_source

    def read(path: Path) -> Any:
        return None if path.name == "0002_add.py" else real(path)

    monkeypatch.setattr(history_module, "_read_source", read)
    odd_history(project, f"    operations = [{ADD}]\n")
    with project.db_blocker.unblock():
        loaded = load_history()
    info = loaded.by_key[("shop", "0002_add")]
    assert not info.editable
    assert info.unlocatable_reason == "source not readable"
    (rec,) = [r for r in loaded.ops if r.migration is info]
    assert rec.node is None


def test_source_file_offsets():
    source = SourceFile(Path("x.py"), "a = [1, 2]")  # no final newline
    node = source.tree.body[0].value  # type: ignore[attr-defined]
    assert source.node_span(node) == (4, 10)


def test_read_missing_source(tmp_path):
    assert _read_source(tmp_path / "missing.py") is None


def test_migration_path_without_file():
    module = types.ModuleType("compiled_migration")
    cls = type("Migration", (), {"__module__": module.__name__})
    import sys

    sys.modules[module.__name__] = module
    try:
        assert migration_path(cls()) is None
    finally:
        del sys.modules[module.__name__]


def test_callee_name():
    call = ast.parse("ops[0]()").body[0].value  # type: ignore[attr-defined]
    assert callee_name(call.func) is None


def test_load_history_with_loader(project):
    from django.db.migrations.loader import MigrationLoader

    project.migration("shop", "0001_initial", ITEM)
    project.activate()
    with project.db_blocker.unblock():
        loader = MigrationLoader(None, ignore_no_migrations=True)
        assert load_history(loader).loader is loader


def test_invalidate_without_bytecode_path(monkeypatch):
    def fail(path: str) -> str:
        raise NotImplementedError

    monkeypatch.setattr(importlib.util, "cache_from_source", fail)
    invalidate_modules([], [Path("x.py")])


def test_migration_source_helper():
    assert "dependencies" in migration_source("", [])


def test_non_ascii_names(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        """
        migrations.AddField("item", "имя", models.IntegerField(default=0)),
        migrations.RunPython(touch),
        """,
        extra="""
        def touch(apps, schema_editor):
            apps.get_model("shop", "Item").objects.filter(имя=1)
        """,
    )
    project.migration("shop", "0003_remove", 'migrations.RemoveField("item", "имя"),')
    result = project.compact()
    assert blocked(result)["shop.Item.имя"] == (
        "RunPython in shop.0002_add mentions 'имя'",
    )


def test_unusual_line_breaks():
    sf = SourceFile(__import__("pathlib").Path("m.py"), "# a b\x0cc\nx = [1]\n")
    assert sf.tree is not None
    node = sf.tree.body[0].value  # type: ignore[attr-defined]
    start, end = sf.node_span(node)
    assert sf.text[start:end] == "[1]"


def test_unusual_line_breaks_in_migration(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        """
        migrations.AddField("item", "legacy", models.IntegerField(default=0)),
        migrations.RunSQL("SELECT 1"),
        """,
        extra="# Odd line\x0cbreaks here.",
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )
    result = project.check_equivalence()
    assert kinds(result) == [("field", "shop.Item.legacy")]
