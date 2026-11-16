from __future__ import annotations

import json
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.core.management import CommandError
from django.db import connections

from django_compact_migrations import profile as profile_module
from django_compact_migrations.core import CompactError
from django_compact_migrations.runner import run_history
from django_compact_migrations.scratch import ScratchDatabaseError, scratch_database
from tests.test_command import history  # noqa: F401
from tests.test_compact import ITEM


def test_profile_report(history):  # noqa: F811
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


def test_profile_json(history):  # noqa: F811
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


def test_hooks_removed(history):  # noqa: F811
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


def test_profile_state_time(history):  # noqa: F811
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
    import os
    import signal

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
