from __future__ import annotations

import pytest
from django.core.management import CommandError

from tests.project import Project
from tests.test_clean import ITEM


@pytest.fixture
def history(project: Project) -> Project:
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


def test_dry_run(history: Project) -> None:
    before = history.files()
    out, err = history.command()
    assert out == (
        "shop.Coupon: model created in 0002_coupon, deleted in 0003_drop (2 operations)\n"
        "shop.Item.legacy: field added in 0002_coupon, removed in 0003_drop (2 operations)\n"
        "Would remove 4 operations in 2 migration files.\n"
    )
    assert err == ""
    assert history.files() == before


def test_verbose(history: Project) -> None:
    out, _ = history.command(verbosity=2)
    lines = out.splitlines()
    assert lines[1].endswith("0002_coupon.py:14  CreateModel")
    assert lines[2].endswith("0003_drop.py:11  DeleteModel")
    assert "Blocked:" in lines
    assert "  shop.Item.rank (field):" in lines
    assert "    - RunPython in shop.0002_coupon mentions 'rank'" in lines


def test_show_blocked(history: Project) -> None:
    out, _ = history.command("--show-blocked")
    assert "    - RunPython in shop.0002_coupon mentions 'rank'" in out.splitlines()


def test_quiet(history: Project) -> None:
    out, err = history.command(verbosity=0)
    assert out == err == ""


def test_check(history: Project) -> None:
    with pytest.raises(SystemExit) as exc_info:
        history.command("--check")
    assert exc_info.value.code == 1
    history.command("--write")
    out, _ = history.command("--check")
    assert out == "No operations to remove.\n"


def test_write(history: Project) -> None:
    out, _ = history.command("--write")
    assert out.endswith("Removed 4 operations in 2 migration files.\n")
    source = history.read("shop", "0003_drop")
    assert "Coupon" not in source
    assert 'migrations.RemoveField("item", "rank"),' in source
    out, _ = history.command()
    assert out == "No operations to remove.\n"


def test_empty_migrations_summary(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "x", models.IntegerField(default=0)),',
    )
    project.migration("shop", "0003_remove", 'migrations.RemoveField("item", "x"),')
    out, _ = project.command()
    assert out.splitlines()[-1] == (
        "Would remove 2 operations in 2 migration files."
        " 2 migrations would be left with no operations."
    )


def test_write_and_check(history: Project) -> None:
    with pytest.raises(CommandError, match="mutually exclusive"):
        history.command("--write", "--check")


def test_unknown_app(history: Project) -> None:
    with pytest.raises(CommandError, match="No installed app with label 'nope'"):
        history.command("nope")


def test_kind_and_exclude(history: Project) -> None:
    out, _ = history.command("--kind", "field")
    assert out.startswith("shop.Item.legacy:")
    out, _ = history.command("--exclude", "shop.Coupon", "--exclude", "shop.Item")
    assert out == "No operations to remove.\n"
    out, _ = history.command("--opaque", "ignore", "shop.Item")
    assert out.count("\n") == 3


def test_warning(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0001_squashed",
        source="""
        from django.db import migrations

        class Migration(migrations.Migration):
            replaces = [("shop", "0001_initial")]
        """,
    )
    _, err = project.command()
    assert err.startswith("Warning: 1 migration(s) replaced")
