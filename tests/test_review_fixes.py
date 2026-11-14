"""Regression tests for reviewed failure scenarios."""

from __future__ import annotations

import pytest

from django_clean_migrations import core
from django_clean_migrations.core import CleanError
from django_clean_migrations.history import SourceFile
from tests.project import Project
from tests.test_clean import ITEM, blocked, kinds

TAG = """
    migrations.CreateModel(
        name="Tag",
        fields=[
            ("id", models.AutoField(primary_key=True)),
            ("code", models.CharField(max_length=5, unique=True)),
        ],
    ),
"""


class TestSupersededReleases:
    def test_to_field_released(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM + TAG)
        project.migration(
            "shop",
            "0003_tag",
            'migrations.AddField("item", "tag", models.ForeignKey("shop.tag",'
            ' models.CASCADE, to_field="code", null=True)),',
        )
        project.migration(
            "shop",
            "0004_alter",
            'migrations.AlterField("item", "tag", models.ForeignKey("shop.tag",'
            " models.CASCADE, null=True)),",
        )
        project.migration(
            "shop", "0005_drop_code", 'migrations.RemoveField("tag", "code"),'
        )
        project.migration(
            "shop",
            "0006_blank",
            'migrations.AlterField("item", "tag", models.ForeignKey("shop.tag",'
            " models.CASCADE, null=True, blank=True)),",
        )
        result = project.check_equivalence()
        assert result.removals == []

    def test_db_column_released(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            'migrations.AddField("item", "a", models.IntegerField(default=0, db_column="legacy")),',
        )
        project.migration(
            "shop",
            "0003_a",
            'migrations.AlterField("item", "a", models.IntegerField(default=0)),',
        )
        project.migration(
            "shop",
            "0004_legacy",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        project.migration(
            "shop",
            "0005_a",
            'migrations.AlterField("item", "a", models.IntegerField(default=1)),',
        )
        result = project.check_equivalence()
        assert result.removals == []

    def test_same_column_still_superseded(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_a",
            'migrations.AlterField("item", "name", models.CharField(max_length=20, db_column="n")),',
        )
        project.migration(
            "shop",
            "0003_b",
            'migrations.AlterField("item", "name", models.CharField(max_length=30, db_column="n")),',
        )
        project.migration(
            "shop",
            "0004_c",
            'migrations.AlterField("item", "name", models.CharField(max_length=40, db_column="n")),',
        )
        result = project.check_equivalence()
        assert [r.kind for r in result.removals] == ["superseded"]


class TestWriteRestores:
    def dead_field(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_add",
            'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
        )
        project.migration(
            "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
        )

    def test_failure_mid_write(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.dead_field(project)
        before = project.files()
        result = project.clean()
        real = core._write_text
        calls = []

        def flaky(path, text):
            calls.append(path)
            if len(calls) == 2:
                path.write_text("garbage")  # a torn write
                raise OSError("disk full")
            real(path, text)

        monkeypatch.setattr(core, "_write_text", flaky)
        with pytest.raises(CleanError, match="restored: disk full"):
            result.write()
        assert project.files() == before

    def test_keyboard_interrupt(
        self, project: Project, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.dead_field(project)
        before = project.files()
        result = project.clean()

        def interrupt(*args: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(core, "verify_on_disk", interrupt)
        with pytest.raises(KeyboardInterrupt):
            result.write()
        assert project.files() == before

    def test_no_temp_files_left(self, project: Project) -> None:
        self.dead_field(project)
        project.clean().write()
        assert sorted(p.name for p in project.path("shop").iterdir()) == [
            "0001_initial.py",
            "0002_add.py",
            "0003_remove.py",
            "__init__.py",
        ]


PERSON = """
    migrations.CreateModel(
        name="Person",
        fields=[("id", models.AutoField(primary_key=True))],
    ),
"""


class TestReverseNames:
    def test_default_query_name(self, project: Project) -> None:
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

    def test_default_related_name(self, project: Project) -> None:
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
        result = project.clean("shop.Item.owner")
        assert blocked(result)["shop.Item.owner"] == (
            "RunSQL in other.0001_data mentions 'things'",
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


class TestElidableScope:
    def test_unordered_elidable_blocks(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM)
        project.migration("shop", "0002_coupon", COUPON)
        project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        project.migration(
            "other",
            "0001_initial",
            "migrations.RunSQL(\"SELECT 'coupon'\", elidable=True),",
            deps=[("shop", "0001_initial")],
        )
        result = project.check_equivalence("shop")
        assert result.removals == []
        assert blocked(result)["shop.Coupon"] == (
            "RunSQL in other.0001_initial mentions 'coupon'",
        )
        assert "RunSQL" in project.read("other", "0001_initial")

    def test_ordered_elidable_reported(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM)
        project.migration(
            "shop",
            "0002_coupon",
            COUPON + "migrations.RunSQL(\"SELECT 'coupon'\", elidable=True),",
        )
        project.migration("shop", "0003_drop", 'migrations.DeleteModel("Coupon"),')
        result = project.check_equivalence()
        assert [r.description for r in result.removals if r.kind == "elidable"] == [
            "elidable RunSQL in 0002_coupon (mentions 'coupon', removed with shop.Coupon)"
        ]

    def test_elidable_in_excluded_app_blocks(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM)
        project.migration("shop", "0002_coupon", COUPON)
        project.migration(
            "other",
            "0001_initial",
            "migrations.RunSQL(\"SELECT 'coupon'\", elidable=True),",
            deps=[("shop", "0002_coupon")],
        )
        project.migration(
            "shop",
            "0003_drop",
            'migrations.DeleteModel("Coupon"),',
            extra_deps=[("other", "0001_initial")],
        )
        result = project.clean("shop", exclude=["other"])
        assert blocked(result)["shop.Coupon"] == (
            "elidable RunSQL in other.0001_initial mentions 'coupon' but cannot be"
            " removed: app 'other' is excluded",
        )

    def test_elidable_flag_respects_exclude(self, project: Project) -> None:
        project.migration(
            "shop",
            "0001_initial",
            ITEM + "migrations.RunSQL('SELECT 1', elidable=True),",
        )
        project.migration(
            "other", "0001_initial", "migrations.RunSQL('SELECT 2', elidable=True),"
        )
        result = project.clean(elidable=True, exclude=["other"])
        assert kinds(result) == [("elidable", "shop")]
        assert (
            result.removals[0].description
            == "elidable RunSQL in 0001_initial (--elidable)"
        )


def test_dependency_names_not_scanned(project: Project) -> None:
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


def test_excluded_child_blocks_model(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM + COUPON)
    project.migration("shop", "0002_drop", 'migrations.DeleteModel("Coupon"),')
    result = project.clean(exclude=["shop.Coupon.code"])
    assert result.removals == []
    assert blocked(result)["shop.Coupon"] == (
        "it contains the excluded field shop.Coupon.code",
    )


def test_deconstructible_without_eq(project: Project) -> None:
    (project.root / project.package / "validators.py").write_text(
        "from django.utils.deconstruct import deconstructible\n\n\n"
        "@deconstructible\nclass Check:\n    def __init__(self, limit=1):\n"
        "        self.limit = limit\n\n    def __call__(self, value):\n        pass\n"
    )
    project.migration(
        "shop",
        "0001_initial",
        f"""
        migrations.CreateModel(
            name="Item",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("name", models.CharField(max_length=5, validators=[{project.package}.validators.Check(2)])),
                ("legacy", models.IntegerField()),
            ],
        ),
        """,
        extra=f"import {project.package}.validators",
    )
    project.migration("shop", "0002_drop", 'migrations.RemoveField("item", "legacy"),')
    result = project.check_equivalence()
    assert kinds(result) == [("field", "shop.Item.legacy")]


def test_non_ascii_names(project: Project) -> None:
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
    result = project.clean()
    assert blocked(result)["shop.Item.имя"] == (
        "RunPython in shop.0002_add mentions 'имя'",
    )


def test_unusual_line_breaks() -> None:
    sf = SourceFile(__import__("pathlib").Path("m.py"), "# a b\x0cc\nx = [1]\n")
    assert sf.tree is not None
    node = sf.tree.body[0].value  # type: ignore[attr-defined]
    start, end = sf.node_span(node)
    assert sf.text[start:end] == "[1]"


def test_unusual_line_breaks_in_migration(project: Project) -> None:
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


class TestUnknownTargets:
    def test_model(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CleanError, match="No model matching 'shop.Nope'"):
            project.clean("shop.Nope")

    def test_field(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CleanError, match="No field matching 'shop.Item.nope'"):
            project.clean("shop.Item.nope")

    def test_exclude(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM)
        with pytest.raises(CleanError, match="No model matching 'shop.Nope'"):
            project.clean(exclude=["shop.Nope"])

    def test_old_names_ok(self, project: Project) -> None:
        project.migration("shop", "0001_initial", ITEM)
        project.migration("shop", "0002_drop", 'migrations.DeleteModel("Item"),')
        assert project.clean("shop.Item.name").removals == []


def test_bom_reason(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    path = project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
    )
    path.write_text("﻿" + path.read_text(), encoding="utf-8")
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )
    result = project.clean()
    assert blocked(result)["shop.Item.legacy"] == (
        "operations list in shop.0002_add could not be parsed"
        " (file starts with a UTF-8 byte order mark)",
    )


def test_summary_wording(project: Project) -> None:
    project.migration(
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
    project.migration("shop", "0002_drop", 'migrations.RemoveField("item", "legacy"),')
    out, _ = project.command()
    assert out.splitlines()[-1] == (
        "Would remove 1 operation and edit 1 operation in 2 migration files."
        " 1 migration would be left with no operations."
    )
    out, _ = project.command("--write")
    assert out.splitlines()[-1] == (
        "Removed 1 operation and edited 1 operation in 2 migration files."
        " 1 migration now has no operations."
    )
