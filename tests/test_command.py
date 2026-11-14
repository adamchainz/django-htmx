from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path
from typing import Any

import pytest
from django.core.management import CommandError

from django_clean_migrations.core import load_plan
from django_clean_migrations.management.commands.clean_migrations import (
    sqlite_order_warning,
)
from tests.project import Project
from tests.test_clean import ITEM
from tests.test_snapshots import pg  # noqa: F401


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
    out, err = history.command("plan")
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
        "Run 'clean_migrations apply' to apply.",
    ]
    assert err == ""
    assert history.files() == before


def test_verbose(history: Project) -> None:
    out, _ = history.command("plan", verbosity=2)
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
    out, _ = history.command("plan", "--show-blocked")
    assert "    RunPython in shop.0002_coupon mentions 'rank'" in out.splitlines()
    assert "0002_coupon.py:14" not in out


def test_plain_output(history: Project) -> None:
    out, _ = history.command("plan", verbosity=2)
    assert "\x1b" not in out


def test_force_color(history: Project) -> None:
    out, _ = history.command("plan", "--force-color")
    assert "\x1b[" in out
    out, _ = history.command("plan", "--no-color")
    assert "\x1b" not in out


def test_quiet(history: Project) -> None:
    out, err = history.command("plan", verbosity=0)
    assert out == err == ""


def test_check(history: Project) -> None:
    with pytest.raises(SystemExit) as exc_info:
        history.command("plan", "--check")
    assert exc_info.value.code == 1
    history.command("apply")
    out, _ = history.command("plan", "--check")
    assert out == "No operations to remove.\n"


def test_write(history: Project) -> None:
    out, _ = history.command("apply")
    assert out.endswith("Removed 4 operations in 2 migration files.\n")
    assert "apply" not in out
    source = history.read("shop", "0003_drop")
    assert "Coupon" not in source
    assert 'migrations.RemoveField("item", "rank"),' in source
    out, _ = history.command("plan")
    assert out == "No operations to remove.\n"


def test_empty_migrations_summary(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "x", models.IntegerField(default=0)),',
    )
    project.migration("shop", "0003_remove", 'migrations.RemoveField("item", "x"),')
    out, _ = project.command("plan")
    assert out.splitlines()[-2] == (
        "Would remove 2 operations in 2 migration files."
        " 2 migrations would be left with no operations."
    )


def test_apply_check_rejected(history: Project) -> None:
    with pytest.raises(CommandError, match="unrecognized arguments: --check"):
        history.command("apply", "--check")


def test_unknown_app(history: Project) -> None:
    with pytest.raises(CommandError, match="No installed app with label 'nope'"):
        history.command("plan", "nope")


def test_kind_and_exclude(history: Project) -> None:
    out, _ = history.command("plan", "--kind", "field")
    assert "legacy" in out
    assert "Coupon" not in out
    out, _ = history.command(
        "plan", "--exclude", "shop.Coupon", "--exclude", "shop.Item"
    )
    assert out == "No operations to remove.\n"
    out, _ = history.command("plan", "--opaque", "ignore", "shop.Item")
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
    _, err = project.command("plan")
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
    out, _ = project.command("plan", "--elidable", verbosity=2)
    lines = out.splitlines()
    assert lines[0] == "shop"
    assert lines[1].split()[:3] == ["RunPython", "elidable", "0002_data"]
    assert "--elidable" in lines[1]
    assert lines[2].strip().endswith("0002_data.py:14  RunPython")


def test_no_subcommand_prints_help(
    history: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    before = history.files()
    history.command()
    out = capsys.readouterr().out
    assert "{plan,apply,profile,scratch}" in out
    assert history.files() == before


def test_options_after_subcommand(history: Project) -> None:
    out, _ = history.command("plan", "-v", "0")
    assert out == ""
    out, _ = history.command("plan", "--no-color", "-v", "2")
    assert "0002_coupon.py:14" in out


def test_plan_file_round_trip(history: Project, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.toml"
    before = history.files()
    out, _ = history.command("plan", "-o", str(plan_path))
    assert f"Saved the plan to {plan_path}" in out
    assert history.files() == before
    data = tomllib.loads(plan_path.read_text())
    assert data["format"] == "django-clean-migrations-plan"
    assert data["version"] == 2
    assert data["verification"] == {"mode": "static"}
    assert data["options"]["opaque"] == "scan"
    assert [f["migration"] for f in data["files"]] == [
        ["shop", "0002_coupon"],
        ["shop", "0003_drop"],
    ]
    assert data["removals"][0]["elements"][0]["operation"] == "CreateModel"
    out, _ = history.command("apply", "--plan", str(plan_path))
    assert out.endswith("Removed 4 operations in 2 migration files.\n")
    assert "Coupon" not in history.read("shop", "0003_drop")
    out, _ = history.command("plan")
    assert out == "No operations to remove.\n"


def test_plan_file_stale(history: Project, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.toml"
    history.command("plan", "-o", str(plan_path))
    path = history.path("shop", "0003_drop")
    path.write_text(path.read_text() + "# edited\n")
    before = history.files()
    with pytest.raises(CommandError, match="changed since the plan was made"):
        history.command("apply", "--plan", str(plan_path))
    assert history.files() == before


def test_plan_file_with_targets(history: Project, tmp_path: Path) -> None:
    plan_path = tmp_path / "plan.toml"
    history.command("plan", "-o", str(plan_path))
    with pytest.raises(CommandError, match="--plan can't be combined"):
        history.command("apply", "--plan", str(plan_path), "shop")


def test_plan_file_invalid(history: Project, tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("not = [toml")
    with pytest.raises(CommandError, match="Could not read plan file"):
        history.command("apply", "--plan", str(bad))
    bad.write_text("")
    with pytest.raises(CommandError, match="is not a django-clean-migrations plan"):
        history.command("apply", "--plan", str(bad))
    bad.write_text('format = "django-clean-migrations-plan"\nversion = 99\n')
    with pytest.raises(CommandError, match="plan format version 99"):
        history.command("apply", "--plan", str(bad))
    bad.write_text('format = "django-clean-migrations-plan"\nversion = 2\n')
    with pytest.raises(CommandError, match="Malformed plan file"):
        history.command("apply", "--plan", str(bad))
    with pytest.raises(CommandError, match="Could not read plan file"):
        history.command("apply", "--plan", str(tmp_path / "missing.toml"))


def test_library_round_trip(history: Project, tmp_path: Path) -> None:
    result = history.clean()
    result.save(tmp_path / "plan.toml", blocked=True)
    history.activate()
    loaded = load_plan(tmp_path / "plan.toml")
    assert loaded.removals == [
        dataclasses.replace(
            r,
            elements=tuple(
                dataclasses.replace(loc, path=loc.path.resolve() if loc.path else None)
                for loc in r.elements
            ),
        )
        for r in result.removals
    ]
    assert loaded.blocked == result.blocked
    assert [f.new for f in loaded.files] == [f.new for f in result.files]
    history.purge()
    loaded.write()
    assert "Coupon" not in history.read("shop", "0003_drop")


def test_apply_failure(history: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    from django_clean_migrations.core import CleanError, Result

    def fail(self: Result) -> None:
        raise CleanError("cannot write")

    monkeypatch.setattr(Result, "write", fail)
    with pytest.raises(CommandError, match="cannot write"):
        history.command("apply")


def test_apply_nothing(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    out, _ = project.command("apply")
    assert out == "No operations to remove.\n"


def test_quiet_plan_file(history: Project, tmp_path: Path) -> None:
    out, err = history.command("plan", "-o", str(tmp_path / "plan.toml"), verbosity=0)
    assert out == err == ""
    assert (tmp_path / "plan.toml").exists()


def test_prune_only_summary(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_nothing")
    project.migration(
        "shop",
        "0003_qty",
        'migrations.AddField("item", "qty", models.IntegerField(default=0)),',
    )
    out, _ = project.command("plan", "--prune")
    assert "1 migration would be deleted." in out.splitlines()


def test_warning_in_color(project: Project) -> None:
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
    _, err = project.command("plan", "--force-color")
    assert "\x1b[" in err


def test_trial_report(history: Project) -> None:
    # On a "terminal": a live status line, and notes printed above it.
    out, _ = history.command_db("plan", "--trial", "--force-color", verbosity=2)
    assert "harvested: shop.0002_coupon RunPython" in out
    assert "SQLite trials can't compare constraint names" in out


def test_trial_masked_tables(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_random",
        'migrations.RunSQL("INSERT INTO shop_item (name) VALUES (hex(randomblob(4)))"),',
    )
    out, _ = project.command_db("plan", "--trial")
    assert "not compared: shop_item." in out


def test_trial_report_postgres(project: Project, pg: str) -> None:  # noqa: F811
    from tests.test_trial import MENTIONED
    from tests.test_trial import history as trial_history

    trial_history(project, MENTIONED)
    out, _ = project.command_db("plan", "--trial", "--database", pg)
    assert "on postgresql" in out
    assert "SQLite trials" not in out


def test_trial_fallback_report(
    history: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    from django_clean_migrations import trial

    def candidate(self: Any, solution: Any, built: Any, reference: Any) -> Any:
        self.passes += 1
        return None if self.passes > 1 else ([], [])

    monkeypatch.setattr(trial.Trial, "_candidate", candidate)
    monkeypatch.setattr(trial.Trial, "_attribute", lambda *args, **kwargs: {})
    out, _ = history.command_db("plan", "--trial")
    assert "The trial didn't converge, so this is the static plan" in out


def test_sqlite_order_warning_other_alias(history: Project) -> None:
    result = history.clean()
    assert sqlite_order_warning(result, "nope") is None


def test_trial_failure_writes_nothing(
    history: Project, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from django_clean_migrations import trial
    from tests.test_trial import FAILURE

    def candidate(self: Any, solution: Any, built: Any, reference: Any) -> Any:
        self.passes += 1
        return ([FAILURE], [])

    monkeypatch.setattr(trial.Trial, "_candidate", candidate)
    monkeypatch.setattr(trial.Trial, "_attribute", lambda *args, **kwargs: {})
    before = history.files()
    plan_path = tmp_path / "plan.toml"
    with pytest.raises(CommandError, match="found no plan"):
        history.command_db("plan", "--trial", "-o", str(plan_path))
    assert not plan_path.exists()
    with pytest.raises(CommandError, match="found no plan"):
        history.command_db("apply", "--trial")
    assert history.files() == before
