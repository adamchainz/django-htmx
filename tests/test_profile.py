from __future__ import annotations

import json
import os
import signal
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.management import CommandError
from django.db import connections
from django.db.models.signals import post_migrate, pre_migrate

from django_compact_migrations import profile
from django_compact_migrations import profile as profile_module
from django_compact_migrations.core import CompactError
from django_compact_migrations.runner import RunError, run_history
from django_compact_migrations.scratch import ScratchDatabaseError, scratch_database
from tests.project import ITEM


def test_profile_report(history):
    out, _ = history.command_db("profile", "--top", "2")
    assert out.startswith("Migrated 3 migrations in ")
    assert "Slowest 2 migrations" in out
    assert "By operation type" in out
    assert "CreateModel" in out
    assert "By app" in out
    assert "The default plan removes 5 operations taking" in out
    assert "post_migrate signals" in out
    assert connections["default"].settings_dict["NAME"] == ":memory:"
    assert settings.DATABASES["default"]["NAME"] == ":memory:"


def test_profile_json(history):
    out, _ = history.command_db("profile", "--json")
    data = json.loads(out)
    assert data["vendor"] == "sqlite"
    assert data["migrations"] == 3
    assert data["removable_operations"] == 5
    assert set(data["by_operation_type"]) == {
        "CreateModel",
        "AddField",
        "RunPython",
        "DeleteModel",
        "RemoveField",
    }
    assert data["by_operation_type"]["AddField"]["count"] == 2
    assert [m["migration"] for m in data["slowest_migrations"]] != []


def test_profile_failure(project):
    project.migration(
        "shop",
        "0001_initial",
        "migrations.RunSQL('SELECT * FROM nowhere'),",
    )
    with pytest.raises(CommandError, match=r"Migrating in shop.0001_initial failed"):
        project.command_db("profile")
    assert connections["default"].settings_dict["NAME"] == ":memory:"


def test_scratch_cleaned_up_on_interrupt(project, monkeypatch):
    project.migration("shop", "0001_initial", "")
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(**kwargs: str) -> str:
        made.append(real_mkdtemp(**kwargs))
        return made[-1]

    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(profile_module, "run_history", interrupt)
    from django.core.management import call_command

    project.activate()
    err = StringIO()
    with project.db_blocker.unblock(), pytest.raises(SystemExit) as info:
        call_command("compact_migrations", "profile", stdout=StringIO(), stderr=err)
    assert info.value.code == 130
    assert err.getvalue() == (
        "Interrupted: scratch databases were dropped and no migration file was"
        " changed.\n"
    )
    assert made and not Path(made[0]).exists()
    assert connections["default"].settings_dict["NAME"] == ":memory:"


def test_scratch_unknown_alias():
    with pytest.raises(ScratchDatabaseError, match="no database alias 'nope'"):
        scratch_database("nope").__enter__()


def test_hooks_removed(history):
    history.activate()
    history.purge()
    with history.db_blocker.unblock(), scratch_database() as connection:
        report = run_history(connection)
    assert len(report.migrations) == 3
    assert len(report.operations) == 8
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None, ignore_no_migrations=True)
    for migration in loader.graph.nodes.values():
        for op in migration.operations:
            assert "database_forwards" not in op.__dict__


def test_profile_state_time(history):
    data = json.loads(history.command_db("profile", "--json")[0])
    assert data["state_seconds"] > 0
    assert data["database_seconds"] > 0
    create = data["by_operation_type"]["CreateModel"]
    assert create["state_seconds"] > 0
    assert set(data["top_state_operations"][0]) == {
        "migration",
        "index",
        "operation",
        "state_seconds",
    }
    out, _ = history.command_db("profile")
    assert "in the database" in out
    assert "updating the project state" in out
    assert "Top 10 operations by project state cost" in out


def test_scratch_listing(project, tmp_path, monkeypatch):
    project.migration("shop", "0001_initial", "")
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    out, _ = project.command_db("scratch")
    assert out == "No scratch databases found.\n"
    leftover = temp / "compactmig_dead"
    leftover.mkdir()
    out, _ = project.command_db("scratch")
    assert out == f"{leftover}\n"
    with pytest.raises(CommandError, match="is not a scratch"):
        project.command_db("scratch", "--drop", str(tmp_path / "other"))
    out, _ = project.command_db("scratch", "--drop-all")
    assert out == f"Dropped {leftover}.\n"
    assert not leftover.exists()


def test_sigterm_cleans_up(project, monkeypatch):

    project.migration("shop", "0001_initial", "")
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(**kwargs: str) -> str:
        made.append(real_mkdtemp(**kwargs))
        return made[-1]

    def terminate(*args: object, **kwargs: object) -> None:
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    monkeypatch.setattr(profile_module, "run_history", terminate)
    with pytest.raises(SystemExit):
        project.command_db("profile")
    assert made and not Path(made[0]).exists()
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_profile_quiet_and_without_plan(project, monkeypatch):
    project.migration("shop", "0001_initial", ITEM)
    out, _ = project.command_db("profile", verbosity=0)
    assert out == ""

    def fail(*args: Any, **kwargs: Any) -> None:
        raise CompactError("no plan")

    monkeypatch.setattr(profile_module, "plan", fail)
    out, _ = project.command_db("profile", "--force-color")
    assert "Could not make the default plan: no plan" in out


def test_profile_without_plan(project, monkeypatch):
    project.migration("shop", "0001_initial", ITEM)
    project.activate()

    def fail(*args: Any, **kwargs: Any) -> None:
        raise CompactError("no plan")

    monkeypatch.setattr(profile, "plan", fail)
    with project.db_blocker.unblock():
        result = profile.profile()
    assert result.removable is None
    assert result.plan_error == "no plan"


def test_run_history_needs_empty_database(project):
    project.migration("shop", "0001_initial", ITEM)
    project.activate()
    with project.db_blocker.unblock(), scratch_database() as connection:
        run_history(connection)
        with pytest.raises(RunError, match="already has applied migrations"):
            run_history(connection)


def test_shared_operation_timed_once(project):
    project.migration("shop", "0001_initial", ITEM)
    (project.path("shop").parent / "shared.py").write_text(
        "from django.db import migrations\n\n"
        "NOTHING = migrations.RunPython(migrations.RunPython.noop)\n"
    )
    extra = f"from {project.package}.shop.shared import NOTHING"
    project.migration("shop", "0002_a", "NOTHING,", extra=extra)
    project.migration("shop", "0003_b", "NOTHING,", extra=extra)
    project.activate()
    with project.db_blocker.unblock(), scratch_database() as connection:
        report = run_history(connection)
    assert [
        t.migration[1] for t in report.operations if t.operation == "RunPython"
    ] == ["0002_a"]


def test_signal_handler_failures(project):
    project.migration("shop", "0001_initial", ITEM)
    (project.path("shop").parent / "models.py").write_text("")
    project.activate()

    def fail(**kwargs: Any) -> None:
        raise ValueError("handler")

    for signal_, name in ((pre_migrate, "pre_migrate"), (post_migrate, "post_migrate")):
        signal_.connect(fail, dispatch_uid="test_signal_handler_failures")
        try:
            with (
                project.db_blocker.unblock(),
                scratch_database() as connection,
                pytest.raises(RunError, match=f"A {name} signal handler failed"),
            ):
                run_history(connection)
        finally:
            signal_.disconnect(dispatch_uid="test_signal_handler_failures")
    assert connections["default"].vendor == "sqlite"
