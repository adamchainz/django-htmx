"""The static analysis's rules for MySQL and MariaDB (see backends)."""

from __future__ import annotations

from typing import Any

from django.db import models
from django.db.models import F
from django.db.models.functions import Lower
from unittest_parametrize import parametrize

from django_compact_migrations import backends
from tests.project import (
    Project,
    ProjectTestCase,
    blocked,
    folds,
    kinds,
    only,
)

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


SERVERS_MY = [backends.MYSQL, backends.MARIADB]


def parametrize_servers(argnames: str | tuple[str, ...], rows: list[Any]) -> Any:
    """parametrize(), for each MySQL-family server (``server``) too."""
    names = (argnames,) if isinstance(argnames, str) else argnames
    rows = [row if isinstance(row, tuple) else (row,) for row in rows]
    return parametrize(
        ("server", *names), [(server, *row) for server in SERVERS_MY for row in rows]
    )


def unique_together_pair(project: Project, entry: str) -> None:
    project.migration("shop", "0001_initial", PARENT + CHILD)
    project.migration(
        "shop", "0002_add", f'migrations.AlterUniqueTogether("child", {{{entry}}}),'
    )
    project.migration(
        "shop", "0003_drop", 'migrations.AlterUniqueTogether("child", set()),'
    )


FK = 'models.ForeignKey("shop.parent", models.CASCADE, null=True{})'


class FakeConnection:
    def __init__(self, vendor: str, mariadb: bool | None) -> None:
        self.vendor = vendor
        self._mariadb = mariadb

    @property
    def mysql_is_mariadb(self) -> bool:
        if self._mariadb is None:
            raise OSError("can't connect")
        return self._mariadb


class BackendsTests(ProjectTestCase):
    @parametrize("server", SERVERS_MY)
    def test_together_on_foreign_key_kept(self, server):
        self.monkeypatch.setattr(backends, "configured", lambda: frozenset({server}))
        unique_together_pair(self.project, '("parent", "x")')
        result = self.project.compact(skip_kinds=["fold"])
        assert result.removals == []
        (reason,) = blocked(result)["shop.Child unique_together (parent, x)"]
        assert reason.startswith(
            f"on {backends.describe([server])}, the index backing the foreign key"
            " shop.Child.parent would get another name"
        )
        # Adding the entry folds into the CreateModel; dropping it doesn't.
        assert [d for _, d in folds(self.project.compact())] == [
            "AlterUniqueTogether in 0002_add folded into CreateModel in 0001_initial"
        ]

    @parametrize("server", SERVERS_MY)
    def test_together_elsewhere_removed(self, server):
        self.monkeypatch.setattr(backends, "configured", lambda: frozenset({server}))
        unique_together_pair(self.project, '("x", "parent")')
        assert {k for k, _ in kinds(self.project.compact())} == {"together"}

    def test_together_without_mysql(self):
        unique_together_pair(self.project, '("parent", "x")')
        assert {k for k, _ in kinds(self.project.compact())} == {"together"}
        assert folds(self.project.compact()) == []

    @parametrize_servers(
        "index",
        [
            'models.Index(fields=["-parent", "x"], name="px")',
            'models.Index(F("parent"), F("x"), name="px")',
            'models.Index(F("parent").desc(), name="px")',
        ],
    )
    def test_index_on_foreign_key_kept(self, server, index):
        self.monkeypatch.setattr(backends, "configured", lambda: frozenset({server}))
        self.project.migration("shop", "0001_initial", PARENT + CHILD)
        self.project.migration(
            "shop",
            "0002_index",
            f"migrations.AddIndex('child', {index}),",
            extra="from django.db.models import F",
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.RemoveIndex("child", "px"),'
        )
        assert list(blocked(self.project.compact())) == ["shop.Child index 'px'"]

    @parametrize("server", SERVERS_MY)
    def test_constraint_on_foreign_key_dropped_later(self, server):
        self.monkeypatch.setattr(backends, "configured", lambda: frozenset({server}))
        self.project.migration("shop", "0001_initial", PARENT + CHILD)
        self.project.migration(
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
        self.project.migration(
            "shop",
            "0003_drop",
            """
            migrations.RemoveConstraint("child", "upx"),
            migrations.RemoveConstraint("child", "positive"),
            migrations.RemoveField("child", "parent"),
            """,
        )
        assert kinds(self.project.compact(**only("constraint"))) == [
            ("constraint", "shop.Child"),
            ("constraint", "shop.Child"),
        ]

    @parametrize("server", SERVERS_MY)
    def test_constraint_folded_past_another_dropped(self, server):
        self.monkeypatch.setattr(backends, "configured", lambda: frozenset({server}))
        self.project.migration(
            "shop",
            "0001_initial",
            PARENT
            + CHILD.replace(
                "    ],\n",
                '    ],\n    options={"constraints": [models.UniqueConstraint('
                'fields=["parent", "x"], name="upx")]},\n',
            ),
        )
        self.project.migration(
            "shop",
            "0002_swap",
            """
            migrations.RemoveConstraint("child", "upx"),
            migrations.AddConstraint(
                "child", models.UniqueConstraint(fields=["parent"], name="up")
            ),
            """,
        )
        result = self.project.compact()
        assert folds(result) == []
        assert result.removals == []

    @parametrize_servers(
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
    def test_alter_field_on_foreign_key(self, server, first, then, safe):
        self.monkeypatch.setattr(backends, "configured", lambda: frozenset({server}))
        self.project.migration("shop", "0001_initial", PARENT + THING)
        # Kept out of the CreateModel by the RunSQL.
        self.project.migration(
            "shop",
            "0002_add",
            f"""
            migrations.RunSQL("SELECT 'thing'"),
            migrations.AddField("thing", "p", {first}),
            """,
        )
        self.project.migration(
            "shop", "0003_alter", f'migrations.AlterField("thing", "p", {then}),'
        )
        self.project.migration(
            "shop",
            "0004_alter",
            f'migrations.AlterField("thing", "p", {then.replace("null=True", "null=True, blank=True")}),',
        )
        result = self.project.compact()
        assert bool(folds(result)) is safe
        assert bool([r for r in result.removals if r.kind == "superseded"]) is safe

    @parametrize_servers(
        "field",
        [
            "models.TextField(default='hello')",
            "models.JSONField(default=dict)",
            "models.BinaryField(default=b'x')",
        ],
    )
    def test_text_default_folds(self, server, field):
        self.monkeypatch.setattr(backends, "configured", lambda: frozenset({server}))
        self.project.migration("shop", "0001_initial", THING)
        self.project.migration(
            "shop", "0002_add", f'migrations.AddField("thing", "t", {field}),'
        )
        self.project.migration(
            "shop",
            "0003_alter",
            f'migrations.AlterField("thing", "t", {field.replace("(", "(blank=True, ")}),',
        )
        folded = bool(folds(self.project.compact()))
        assert folded is (server == backends.MARIADB)

    @parametrize("server", SERVERS_MY)
    def test_empty_text_default_folds(self, server):
        self.monkeypatch.setattr(backends, "configured", lambda: frozenset({server}))
        self.project.migration("shop", "0001_initial", THING)
        self.project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("thing", "t", models.TextField(default="")),',
        )
        assert folds(self.project.compact())

    @parametrize("server", SERVERS_MY)
    def test_text_alters_on_mysql(self, server):
        self.monkeypatch.setattr(backends, "configured", lambda: frozenset({server}))
        self.project.migration(
            "shop",
            "0001_initial",
            THING + 'migrations.AddField("thing", "t", models.TextField(default="a")),',
        )
        self.project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("thing", "t", models.TextField(default="b")),',
        )
        self.project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("thing", "t", models.TextField(default="c")),',
        )
        result = self.project.compact()
        assert bool(result.removals) is (server == backends.MARIADB)

    def test_first_field_name(self):
        assert (
            backends.first_field_name(models.Index(fields=["-a", "b"], name="i")) == "a"
        )
        assert backends.first_field_name(models.Index(F("a").asc(), name="i")) == "a"
        assert backends.first_field_name(models.Index(Lower("a"), name="i")) is None
        assert backends.first_field_name(None) is None

    @parametrize("server", SERVERS_MY)
    def test_other_folds_and_removals(self, server):
        self.monkeypatch.setattr(backends, "configured", lambda: frozenset({server}))
        self.project.migration("shop", "0001_initial", PARENT + CHILD)
        self.project.migration(
            "shop",
            "0002_more",
            """
            migrations.AlterUniqueTogether("child", {("x", "parent")}),
            migrations.AddField("child", "legacy", models.IntegerField(default=0)),
            """,
        )
        self.project.migration(
            "shop", "0003_drop", 'migrations.RemoveField("child", "legacy"),'
        )
        result = self.project.compact()
        assert ("field", "shop.Child.legacy") in kinds(result)
        assert [d for _, d in folds(result)] == [
            "AlterUniqueTogether in 0002_more folded into CreateModel in 0001_initial"
        ]

    def test_configured(self):
        fake = {
            "default": FakeConnection("postgresql", None),
            "a": FakeConnection("mysql", True),
            "b": FakeConnection("mysql", None),
        }
        self.monkeypatch.setattr(backends, "connections", fake)
        assert backends.configured() == {backends.MARIADB, backends.MYSQL}
        del fake["b"]
        assert backends.configured() == {backends.MARIADB}
