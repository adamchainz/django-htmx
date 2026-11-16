from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from django_compact_migrations import worktree
from tests.project import ITEM, Project, server_alias


@pytest.fixture(autouse=True)
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    # Files are rewritten within the same second as they are first imported.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


@pytest.fixture
def project(
    tmp_path: Path, django_db_blocker: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Project]:
    # The command edits migrations under the project root only (the current
    # directory's Git work tree); the forked runs' coverage data needs the
    # current directory left alone.
    monkeypatch.setattr(worktree, "project_root", lambda: tmp_path.resolve())
    p = Project(tmp_path, django_db_blocker)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def pg(django_db_blocker: Any) -> Iterator[str]:
    """The PostgreSQL server from compose.yaml."""
    with server_alias("pg", django_db_blocker) as alias:
        yield alias


@pytest.fixture(params=["mysql", "mariadb"])
def my(request: Any, django_db_blocker: Any) -> Iterator[str]:
    """The MySQL and the MariaDB server from compose.yaml."""
    with server_alias(request.param, django_db_blocker) as alias:
        yield alias


@pytest.fixture
def history(project: Project) -> Project:
    """shop: a dead model, a dead field, and a field a RunPython uses."""
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_coupon",
        """
        migrations.CreateModel(
            name="Coupon",
            fields=[("id", models.AutoField(primary_key=True))],
        ),
        migrations.AddField("item", "legacy", models.IntegerField(default=0)),
        migrations.AddField("item", "rank", models.IntegerField(default=0)),
        migrations.RunPython(rank),
        """,
        extra="""
        def rank(apps, schema_editor):
            apps.get_model("shop", "Item").objects.update(rank=1)
        """,
    )
    project.migration(
        "shop",
        "0003_drop",
        """
        migrations.DeleteModel("Coupon"),
        migrations.RemoveField("item", "legacy"),
        migrations.RemoveField("item", "rank"),
        """,
    )
    return project
