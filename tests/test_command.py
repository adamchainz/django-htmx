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
    lines = out.splitlines()
    assert lines[0] == "shop"
    assert lines[1].split() == ["Coupon", "model", "0002_coupon", "->", "0003_drop"] + [
        "2",
        "operations",
    ]
    assert lines[2] == "  Item"
    assert lines[3].split() == ["legacy", "field", "0002_coupon", "->", "0003_drop"] + [
        "2",
        "operations",
    ]
    assert lines[3].startswith("    legacy")
    assert lines[4:] == [
        "Would remove 4 operations in 2 migration files.",
        "Run with --write to apply.",
    ]
    assert err == ""
    assert history.files() == before


def test_verbose(history: Project) -> None:
    out, _ = history.command(verbosity=2)
    lines = out.splitlines()
    assert lines[2].strip().endswith("0002_coupon.py:14  CreateModel")
    assert lines[3].strip().endswith("0003_drop.py:11  DeleteModel")
    assert "Blocked" in lines
    assert any(
        line.startswith("  shop.Item.rank")
        and "field added in 0002_coupon, removed in 0003_drop" in line
        for line in lines
    )
    assert "    RunPython in shop.0002_coupon mentions 'rank'" in lines


def test_show_blocked(history: Project) -> None:
    out, _ = history.command("--show-blocked")
    assert "    RunPython in shop.0002_coupon mentions 'rank'" in out.splitlines()
    assert "0002_coupon.py:14" not in out


def test_plain_output(history: Project) -> None:
    out, _ = history.command(verbosity=2)
    assert "\x1b" not in out


def test_force_color(history: Project) -> None:
    out, _ = history.command("--force-color")
    assert "\x1b[" in out
    out, _ = history.command("--no-color")
    assert "\x1b" not in out


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
    assert "--write" not in out
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
    assert out.splitlines()[-2] == (
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
    assert "legacy" in out
    assert "Coupon" not in out
    out, _ = history.command("--exclude", "shop.Coupon", "--exclude", "shop.Item")
    assert out == "No operations to remove.\n"
    out, _ = history.command("--opaque", "ignore", "shop.Item")
    assert "rank" in out
    assert "Coupon" not in out


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


def test_elidable(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_data",
        "migrations.RunPython(noop, elidable=True),",
        extra="""
        def noop(apps, schema_editor):
            pass
        """,
    )
    out, _ = project.command("--elidable", verbosity=2)
    lines = out.splitlines()
    assert lines[0] == "shop"
    assert lines[1].split()[:3] == ["RunPython", "elidable", "0002_data"]
    assert "--elidable" in lines[1]
    assert lines[2].strip().endswith("0002_data.py:14  RunPython")
