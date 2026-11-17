from __future__ import annotations

import dataclasses
import tomllib
from io import StringIO
from typing import Any

import pytest
from django.core.management import CommandError, call_command

from django_compact_migrations.core import CompactError, Result
from django_compact_migrations.management.commands.compact_migrations import (
    sqlite_order_warning,
)
from django_compact_migrations.planfile import load_plan, save_plan
from tests.project import (
    FAILURE,
    ITEM,
    MENTIONED,
    with_data_migration,
)

PLAN_OUTPUT = """\
shop
  Coupon    model  0002_coupon -> 0003_drop     2 operations
  Item
    legacy  field  0002_coupon -> 0003_drop     2 operations
    rank    fold   0002_coupon -> 0001_initial   1 operation
"""


def test_dry_run(history):
    before = history.files()
    out, err = history.command("plan", "--static")
    assert out == PLAN_OUTPUT + (
        "Checked against Django's model state only, not against a database"
        " (--static): without it, a trial migrates scratch databases to check"
        " the plan.\n"
        "Would remove 4 operations, fold 1 operation and edit 1 operation in 3"
        " migration files.\n"
        "Would edit migration files of: shop.\n"
        "No MySQL or MariaDB database is configured: if production runs on one,"
        " make the plan with it configured (some of its index names depend on"
        " the history).\n"
        "On SQLite, this plan may change the physical order of columns in rebuilt"
        " tables (harmless for Django); a trial (without --static) checks it.\n"
        "Run 'compact_migrations apply' to apply.\n"
    )
    assert err == ""
    assert history.files() == before


def test_verbose(history):
    out, _ = history.command("plan", "--static", verbosity=2)
    lines = out.splitlines()
    assert lines[2].strip().endswith("0002_coupon.py:14  CreateModel")
    assert lines[3].strip().endswith("0003_drop.py:11  DeleteModel")
    assert lines[9].strip().endswith("0002_coupon.py:19  AddField")
    assert lines[10].strip().endswith("0001_initial.py:10  CreateModel (regenerated)")
    assert "Blocked" in lines
    # Folded into the CreateModel, the field is now added there.
    assert "  shop.Item.rank  field added in 0001_initial, removed in 0003_drop" in (
        lines
    )
    assert "    RunPython in shop.0002_coupon mentions 'rank'" in lines


def test_plain_output(history):
    out, _ = history.command("plan", "--static", verbosity=2)
    assert "\x1b" not in out


def test_force_color(history):
    out, _ = history.command("plan", "--static", "--force-color")
    assert "\x1b[" in out
    out, _ = history.command("plan", "--static", "--no-color")
    assert "\x1b" not in out


def test_quiet(history):
    out, err = history.command("plan", "--static", verbosity=0)
    assert out == err == ""
    out, err = history.command("apply", "--static", verbosity=0)
    assert out == err == ""
    assert "Coupon" not in history.read("shop", "0003_drop")


def test_check(history):
    with pytest.raises(SystemExit) as exc_info:
        history.command("plan", "--static", "--check")
    assert exc_info.value.code == 1
    history.command("apply", "--static")
    out, _ = history.command("plan", "--static", "--check")
    assert out == "No operations to remove.\n"


def test_write(history):
    out, _ = history.command("apply", "--static")
    # Warnings before writing; the summary only once the files are written.
    assert out == PLAN_OUTPUT + (
        "Checked against Django's model state only, not against a database"
        " (--static): without it, a trial migrates scratch databases to check"
        " the plan.\n"
        "No MySQL or MariaDB database is configured: if production runs on one,"
        " make the plan with it configured (some of its index names depend on"
        " the history).\n"
        "On SQLite, this plan may change the physical order of columns in rebuilt"
        " tables (harmless for Django); a trial (without --static) checks it.\n"
        "The migration files aren't in a Git work tree, so review and keep a copy"
        " of them yourself.\n"
        "Editing migration files of: shop.\n"
        "Removed 4 operations, folded 1 operation and edited 1 operation in 3"
        " migration files.\n"
    )
    assert "('rank', models.IntegerField(default=0))" in history.read(
        "shop", "0001_initial"
    )
    source = history.read("shop", "0003_drop")
    assert "Coupon" not in source
    assert 'migrations.RemoveField("item", "rank"),' in source
    out, _ = history.command("plan", "--static")
    assert out == "No operations to remove.\n"


def test_empty_migrations_summary(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "x", models.IntegerField(default=0)),',
    )
    project.migration("shop", "0003_remove", 'migrations.RemoveField("item", "x"),')
    out, _ = project.command("plan", "--static")
    assert out.splitlines()[-4] == (
        "Would remove 2 operations in 2 migration files."
        " 2 migrations would be left with no operations."
    )


def test_apply_check_rejected(history):
    with pytest.raises(CommandError, match="unrecognized arguments: --check"):
        history.command("apply", "--static", "--check")


def test_unknown_app(history):
    with pytest.raises(CommandError, match="No installed app with label 'nope'"):
        history.command("plan", "--static", "nope")


def test_skip_kind_and_exclude(history):
    out, _ = history.command(
        "plan", "--static", "--skip-kind", "model", "--skip-kind", "fold"
    )
    assert "legacy" in out
    assert "Coupon" not in out
    out, _ = history.command(
        "plan", "--static", "--exclude", "shop.Coupon", "--exclude", "shop.Item"
    )
    assert out == "No operations to remove.\n"


def test_warning(project):
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
    _, err = project.command("plan", "--static")
    assert err.startswith("Warning: 1 migration(s) replaced")


def test_elidable(project):
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
    out, _ = project.command("plan", "--static", "--elidable", verbosity=2)
    lines = out.splitlines()
    assert lines[0] == "shop"
    assert lines[1].split()[:3] == ["RunPython", "elidable", "0002_data"]
    assert "--elidable" in lines[1]
    assert lines[2].strip().endswith("0002_data.py:14  RunPython")


def test_no_subcommand_prints_help(history, capsys):
    before = history.files()
    history.command()
    out = capsys.readouterr().out
    assert "{plan,apply,scratch}" in out
    assert history.files() == before


def test_options_after_subcommand(history):
    out, _ = history.command("plan", "--static", "-v", "0")
    assert out == ""
    out, _ = history.command("plan", "--static", "--no-color", "-v", "2")
    assert "0002_coupon.py:14" in out


def test_plan_file_round_trip(history, tmp_path):
    plan_path = tmp_path / "plan.toml"
    before = history.files()
    out, _ = history.command("plan", "--static", "-o", str(plan_path))
    assert f"Saved the plan to {plan_path}" in out
    assert history.files() == before
    data = tomllib.loads(plan_path.read_text())
    assert data["format"] == "django-compact-migrations-plan"
    assert data["version"] == 1
    assert data["verification"] == {"mode": "static"}
    assert [f["migration"] for f in data["files"]] == [
        ["shop", "0001_initial"],
        ["shop", "0002_coupon"],
        ["shop", "0003_drop"],
    ]
    assert data["removals"][0]["elements"][0]["operation"] == "CreateModel"
    out, _ = history.command("apply", "--plan", str(plan_path))
    assert out.startswith(PLAN_OUTPUT)
    assert out.splitlines()[-1].startswith("Removed 4 operations, folded 1 operation")
    assert "Coupon" not in history.read("shop", "0003_drop")
    out, _ = history.command("plan", "--static")
    assert out == "No operations to remove.\n"


def test_no_summary_when_writing_fails(history, monkeypatch):
    def fail(self: Result, **kwargs: Any) -> None:
        raise CompactError("boom")

    monkeypatch.setattr(Result, "write", fail)
    out = StringIO()
    history.activate()
    with history.db_blocker.unblock(), pytest.raises(CommandError, match="boom"):
        call_command("compact_migrations", "apply", "--static", stdout=out)
    assert out.getvalue().startswith(PLAN_OUTPUT)
    assert "Removed" not in out.getvalue()


def test_plan_file_stale(history, tmp_path):
    plan_path = tmp_path / "plan.toml"
    history.command("plan", "--static", "-o", str(plan_path))
    path = history.path("shop", "0003_drop")
    path.write_text(path.read_text() + "# edited\n")
    before = history.files()
    with pytest.raises(CommandError, match="changed since the plan was made"):
        history.command("apply", "--plan", str(plan_path))
    assert history.files() == before


def test_plan_file_with_targets(history, tmp_path):
    plan_path = tmp_path / "plan.toml"
    history.command("plan", "--static", "-o", str(plan_path))
    with pytest.raises(CommandError, match="--plan can't be combined"):
        history.command("apply", "--plan", str(plan_path), "shop")


def test_plan_file_invalid(history, tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("not = [toml")
    with pytest.raises(CommandError, match="Could not read plan file"):
        history.command("apply", "--plan", str(bad))
    bad.write_text("")
    with pytest.raises(CommandError, match="is not a django-compact-migrations plan"):
        history.command("apply", "--plan", str(bad))
    bad.write_text('format = "django-compact-migrations-plan"\nversion = 99\n')
    with pytest.raises(CommandError, match="plan format version 99"):
        history.command("apply", "--plan", str(bad))
    bad.write_text('format = "django-compact-migrations-plan"\nversion = 1\n')
    with pytest.raises(CommandError, match="Malformed plan file"):
        history.command("apply", "--plan", str(bad))
    with pytest.raises(CommandError, match="Could not read plan file"):
        history.command("apply", "--plan", str(tmp_path / "missing.toml"))


def test_library_round_trip(history, tmp_path):
    result = history.compact()
    save_plan(result, tmp_path / "plan.toml", blocked=True)
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


def test_apply_failure(history, monkeypatch):
    from django_compact_migrations.core import Result

    def fail(self: Result, **kwargs: Any) -> None:
        raise CompactError("cannot write")

    monkeypatch.setattr(Result, "write", fail)
    with pytest.raises(CommandError, match="cannot write"):
        history.command("apply", "--static")


def test_apply_nothing(project):
    project.migration("shop", "0001_initial", ITEM)
    out, _ = project.command("apply", "--static")
    assert out == "No operations to remove.\n"


def test_quiet_plan_file(history, tmp_path):
    out, err = history.command(
        "plan", "--static", "-o", str(tmp_path / "plan.toml"), verbosity=0
    )
    assert out == err == ""
    assert (tmp_path / "plan.toml").exists()


def test_prune_only_summary(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration("shop", "0002_nothing")
    project.migration(
        "shop",
        "0003_qty",
        'migrations.AddField("item", "qty", models.IntegerField(default=0)),',
    )
    # Nothing removed or folded: only the summary of pruning.
    out, _ = project.command("plan", "--static", "--prune", "--skip-kind", "fold")
    assert "1 migration would be deleted." in out.splitlines()


def test_warning_in_color(project):
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
    _, err = project.command("plan", "--static", "--force-color")
    assert "\x1b[" in err


def test_trial_report(history):
    # On a "terminal": a live status line, and notes printed above it.
    out, _ = history.command_db("plan", "--force-color", verbosity=2)
    assert "harvested: shop.0002_coupon RunPython" in out
    assert "SQLite trials can't compare the names of unique and" in out


def test_trial_masked_tables(project):
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_random",
        'migrations.RunSQL("INSERT INTO shop_item (name) VALUES (hex(randomblob(4)))"),',
    )
    out, _ = project.command_db("plan")
    assert "not compared: shop_item." in out


def test_trial_report_postgres(project, pg):
    with_data_migration(project, MENTIONED)
    out, _ = project.command_db("plan", "--database", pg)
    assert "on PostgreSQL" in out
    assert "SQLite trials" not in out


def test_trial_fallback_report(history, monkeypatch):
    from django_compact_migrations import trial

    def candidate(self: Any, solution: Any, built: Any, reference: Any) -> Any:
        self.passes += 1
        return None if self.passes > 1 else ([], [])

    monkeypatch.setattr(trial.Trial, "_candidate", candidate)
    monkeypatch.setattr(trial.Trial, "_attribute", lambda *args, **kwargs: {})
    out, _ = history.command_db("plan")
    assert "The trial didn't converge, so this is the static plan" in out


def test_sqlite_order_warning_other_alias(history):
    result = history.compact()
    assert sqlite_order_warning(result, "nope") is None


def test_trial_failure_writes_nothing(history, monkeypatch, tmp_path):
    from django_compact_migrations import trial

    def candidate(self: Any, solution: Any, built: Any, reference: Any) -> Any:
        self.passes += 1
        return ([FAILURE], [])

    monkeypatch.setattr(trial.Trial, "_candidate", candidate)
    monkeypatch.setattr(trial.Trial, "_attribute", lambda *args, **kwargs: {})
    before = history.files()
    plan_path = tmp_path / "plan.toml"
    with pytest.raises(CommandError, match="found no plan"):
        history.command_db("plan", "-o", str(plan_path))
    assert not plan_path.exists()
    with pytest.raises(CommandError, match="found no plan"):
        history.command_db("apply")
    assert history.files() == before


def test_summary_wording(project):
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
    out, _ = project.command("plan", "--static")
    assert out.splitlines()[-4] == (
        "Would remove 1 operation and edit 1 operation in 2 migration files."
        " 1 migration would be left with no operations."
    )
    out, _ = project.command("apply", "--static")
    assert out.splitlines()[-1] == (
        "Removed 1 operation and edited 1 operation in 2 migration files."
        " 1 migration now has no operations."
    )


def test_backends_changed_since_the_plan(history, monkeypatch):
    from django_compact_migrations import backends

    found = iter([frozenset(), frozenset({"mariadb"})])
    monkeypatch.setattr(backends, "configured", lambda: next(found))
    out, _ = history.command("plan", "--static")
    assert (
        "This plan was made with no MySQL or MariaDB database configured, and"
        " MariaDB is configured now: some index names on MySQL and MariaDB depend"
        " on the history, so make the plan again.\n"
    ) in out


def test_backends_configured(history, monkeypatch):
    from django_compact_migrations import backends

    monkeypatch.setattr(backends, "configured", lambda: frozenset({"mysql"}))
    out, _ = history.command("plan", "--static")
    assert "MySQL" not in out


# Databases the plan is applied for


def test_partly_migrated_database_refused(history):
    from django.db import connections
    from django.db.migrations.recorder import MigrationRecorder

    history.activate()
    with history.db_blocker.unblock():
        recorder = MigrationRecorder(connections["default"])
        recorder.record_applied("shop", "0001_initial")
        try:
            before = history.files()
            with pytest.raises(
                CommandError,
                match=r"'default' hasn't applied shop.0002_coupon, shop.0003_drop\.",
            ):
                history.command("apply", "--static")
            assert history.files() == before
            out, _ = history.command("apply", "--static", "--no-database-check")
            assert "Removed 4 operations" in out
        finally:
            recorder.migration_qs.all().delete()


def test_database_not_reachable(history, monkeypatch):
    from django.db.utils import ConnectionHandler

    from django_compact_migrations import core

    handler = ConnectionHandler(
        {
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
            "nowhere": {"ENGINE": "django.db.backends.dummy"},
        }
    )
    monkeypatch.setattr(core, "connections", handler)
    out, _ = history.command("apply", "--static")
    assert "Couldn't check which migrations the database 'nowhere' has applied" in out


def test_trial_preface(history):
    out, _ = history.command_db("plan")
    assert out.startswith(
        "The trial creates scratch databases next to 'default' (SQLite, files in"
        " the temporary directory; settings tests.settings) and runs every"
        " migration there, data migrations included: whatever they do outside"
        " the database (email, files, network calls) happens for real.\n"
    )
