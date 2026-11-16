"""The static analysis's rules for MySQL and MariaDB (see backends)."""

from __future__ import annotations

from typing import Any

import pytest
from django.db import models
from django.db.models import F
from django.db.models.functions import Lower

from django_compact_migrations import backends
from tests.project import Project
from tests.test_compact import blocked, kinds
from tests.test_fold import folds

PARENT = (
    'migrations.CreateModel("Parent", [("id", models.AutoField(primary_key=True))]),'
)
CHILD = """
migrations.CreateModel(
    "Child",
    [
        ("id", models.AutoField(primary_key=True)),
        ("x", models.IntegerField()),
        ("parent", models.ForeignKey("shop.parent", models.CASCADE)),
    ],
),
"""
THING = 'migrations.CreateModel("Thing", [("id", models.AutoField(primary_key=True))]),'


@pytest.fixture(params=[backends.MYSQL, backends.MARIADB])
def server(request: Any, monkeypatch: pytest.MonkeyPatch) -> str:
    """A configured MySQL-family database (as backends.configured() finds)."""
    monkeypatch.setattr(backends, "configured", lambda: frozenset({request.param}))
    return str(request.param)


def unique_together_pair(project: Project, entry: str) -> None:
    project.migration("shop", "0001_initial", PARENT + CHILD)
    project.migration(
        "shop", "0002_add", f'migrations.AlterUniqueTogether("child", {{{entry}}}),'
    )
    project.migration(
        "shop", "0003_drop", 'migrations.AlterUniqueTogether("child", set()),'
    )


def test_together_on_foreign_key_kept(project, server):
    unique_together_pair(project, '("parent", "x")')
    result = project.compact()
    assert result.removals == []
    (reason,) = blocked(result)["shop.Child unique_together (parent, x)"]
    assert reason.startswith(
        f"on {backends.describe([server])}, the index backing the foreign key"
        " shop.Child.parent would get another name"
    )
    # Adding the entry folds into the CreateModel; dropping it doesn't.
    assert [d for _, d in folds(project.compact(skip_kinds=[]))] == [
        "AlterUniqueTogether in 0002_add folded into CreateModel in 0001_initial"
    ]


def test_together_elsewhere_removed(project, server):
    unique_together_pair(project, '("x", "parent")')
    assert {k for k, _ in kinds(project.compact())} == {"together"}


def test_together_without_mysql(project):
    unique_together_pair(project, '("parent", "x")')
    assert {k for k, _ in kinds(project.compact())} == {"together"}
    assert folds(project.compact(skip_kinds=[])) == []


@pytest.mark.parametrize(
    "index",
    [
        'models.Index(fields=["-parent", "x"], name="px")',
        'models.Index(F("parent"), F("x"), name="px")',
        'models.Index(F("parent").desc(), name="px")',
    ],
)
def test_index_on_foreign_key_kept(project, server, index):
    project.migration("shop", "0001_initial", PARENT + CHILD)
    project.migration(
        "shop",
        "0002_index",
        f"migrations.AddIndex('child', {index}),",
        extra="from django.db.models import F",
    )
    project.migration("shop", "0003_drop", 'migrations.RemoveIndex("child", "px"),')
    assert list(blocked(project.compact())) == ["shop.Child index 'px'"]


def test_constraint_on_foreign_key_dropped_later(project, server):
    # The foreign key's column goes later anyway: nothing to keep.
    project.migration("shop", "0001_initial", PARENT + CHILD)
    project.migration(
        "shop",
        "0002_constraint",
        """
        migrations.AddConstraint(
            "child", models.UniqueConstraint(fields=["parent", "x"], name="upx")
        ),
        migrations.AddConstraint(
            "child", models.CheckConstraint(condition=Q(x__gt=0), name="positive")
        ),
        """,
        extra="from django.db.models import Q",
    )
    project.migration(
        "shop",
        "0003_drop",
        """
        migrations.RemoveConstraint("child", "upx"),
        migrations.RemoveConstraint("child", "positive"),
        migrations.RemoveField("child", "parent"),
        """,
    )
    assert kinds(project.compact(kinds=["constraint"])) == [
        ("constraint", "shop.Child"),
        ("constraint", "shop.Child"),
    ]


def test_constraint_folded_past_another_dropped(project, server):
    # Created in the CreateModel, the new constraint would exist when the
    # old one is dropped, and Django wouldn't create the key's own index.
    project.migration(
        "shop",
        "0001_initial",
        PARENT
        + CHILD.replace(
            "    ],\n",
            '    ],\n    options={"constraints": [models.UniqueConstraint('
            'fields=["parent", "x"], name="upx")]},\n',
        ),
    )
    project.migration(
        "shop",
        "0002_swap",
        """
        migrations.RemoveConstraint("child", "upx"),
        migrations.AddConstraint(
            "child", models.UniqueConstraint(fields=["parent"], name="up")
        ),
        """,
    )
    result = project.compact(skip_kinds=[])
    assert folds(result) == []
    assert result.removals == []


FK = 'models.ForeignKey("shop.parent", models.CASCADE, null=True{})'


@pytest.mark.parametrize(
    ("first", "then", "safe"),
    [
        # Which index backs the key changes.
        ('models.IntegerField(null=True, db_column="p_id")', FK.format(""), False),
        (FK.format(""), FK.format(", db_constraint=False"), False),
        (FK.format(""), FK.format(", unique=True"), False),
        # It doesn't.
        (FK.format(""), FK.format("").replace("CASCADE", "PROTECT"), True),
    ],
)
def test_alter_field_on_foreign_key(project, server, first, then, safe):
    project.migration("shop", "0001_initial", PARENT + THING)
    # Kept out of the CreateModel by the RunSQL.
    project.migration(
        "shop",
        "0002_add",
        f"""
        migrations.RunSQL("SELECT 'thing'"),
        migrations.AddField("thing", "p", {first}),
        """,
    )
    project.migration(
        "shop", "0003_alter", f'migrations.AlterField("thing", "p", {then}),'
    )
    project.migration(
        "shop",
        "0004_alter",
        f'migrations.AlterField("thing", "p", {then.replace("null=True", "null=True, blank=True")}),',
    )
    result = project.compact(skip_kinds=[])
    assert bool(folds(result)) is safe
    assert bool([r for r in result.removals if r.kind == "superseded"]) is safe


@pytest.mark.parametrize(
    "field",
    [
        "models.TextField(default='hello')",
        "models.JSONField(default=dict)",
        "models.BinaryField(default=b'x')",
    ],
)
def test_text_default_folds(project, server, field):
    # MySQL leaves the DEFAULT of AddField on the column; MariaDB doesn't.
    project.migration("shop", "0001_initial", THING)
    project.migration(
        "shop", "0002_add", f'migrations.AddField("thing", "t", {field}),'
    )
    project.migration(
        "shop",
        "0003_alter",
        f'migrations.AlterField("thing", "t", {field.replace("(", "(blank=True, ")}),',
    )
    folded = bool(folds(project.compact(skip_kinds=[])))
    assert folded is (server == backends.MARIADB)


def test_empty_text_default_folds(project, server):
    project.migration("shop", "0001_initial", THING)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("thing", "t", models.TextField(default="")),',
    )
    assert folds(project.compact(skip_kinds=[]))


def test_text_alters_on_mysql(project, server):
    project.migration(
        "shop",
        "0001_initial",
        THING + 'migrations.AddField("thing", "t", models.TextField(default="a")),',
    )
    project.migration(
        "shop",
        "0002_a",
        'migrations.AlterField("thing", "t", models.TextField(default="b")),',
    )
    project.migration(
        "shop",
        "0003_b",
        'migrations.AlterField("thing", "t", models.TextField(default="c")),',
    )
    result = project.compact()
    assert bool(result.removals) is (server == backends.MARIADB)


def test_first_field_name():
    assert backends.first_field_name(models.Index(fields=["-a", "b"], name="i")) == "a"
    assert backends.first_field_name(models.Index(F("a").asc(), name="i")) == "a"
    assert backends.first_field_name(models.Index(Lower("a"), name="i")) is None
    assert backends.first_field_name(None) is None


def test_other_folds_and_removals(project, server):
    project.migration("shop", "0001_initial", PARENT + CHILD)
    project.migration(
        "shop",
        "0002_more",
        """
        migrations.AlterUniqueTogether("child", {("x", "parent")}),
        migrations.AddField("child", "legacy", models.IntegerField(default=0)),
        """,
    )
    project.migration("shop", "0003_drop", 'migrations.RemoveField("child", "legacy"),')
    result = project.compact(skip_kinds=[])
    assert ("field", "shop.Child.legacy") in kinds(result)
    assert [d for _, d in folds(result)] == [
        "AlterUniqueTogether in 0002_more folded into CreateModel in 0001_initial"
    ]


class FakeConnection:
    def __init__(self, vendor: str, mariadb: bool | None) -> None:
        self.vendor = vendor
        self._mariadb = mariadb

    @property
    def mysql_is_mariadb(self) -> bool:
        if self._mariadb is None:
            raise OSError("can't connect")
        return self._mariadb


def test_configured(monkeypatch):
    fake = {
        "default": FakeConnection("postgresql", None),
        "a": FakeConnection("mysql", True),
        "b": FakeConnection("mysql", None),
    }
    monkeypatch.setattr(backends, "connections", fake)
    assert backends.configured() == {backends.MARIADB, backends.MYSQL}
    del fake["b"]
    assert backends.configured() == {backends.MARIADB}
