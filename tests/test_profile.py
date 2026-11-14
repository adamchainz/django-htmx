from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import CommandError
from django.db import connections

from django_clean_migrations import profile as profile_module
from django_clean_migrations.runner import run_history
from django_clean_migrations.scratch import ScratchDatabaseError, scratch_database
from tests.project import Project
from tests.test_command import history  # noqa: F401


def test_profile_report(history: Project) -> None:  # noqa: F811
    out, _ = history.command_db("profile", "--top", "2")
    assert out.startswith("Migrated 3 migrations in ")
    assert "Slowest 2 migrations" in out
    assert "By operation type" in out
    assert "CreateModel" in out
    assert "By app" in out
    assert "The default plan removes 4 operations taking" in out
    assert "post_migrate signals" in out
    assert connections["default"].settings_dict["NAME"] == ":memory:"
    assert settings.DATABASES["default"]["NAME"] == ":memory:"


def test_profile_json(history: Project) -> None:  # noqa: F811
    out, _ = history.command_db("profile", "--json")
    data = json.loads(out)
    assert data["vendor"] == "sqlite"
    assert data["migrations"] == 3
    assert data["removable_operations"] == 4
    assert set(data["by_operation_type"]) == {
        "CreateModel",
        "AddField",
        "RunPython",
        "DeleteModel",
        "RemoveField",
    }
    assert data["by_operation_type"]["AddField"]["count"] == 2
    assert [m["migration"] for m in data["slowest_migrations"]] != []


def test_profile_failure(project: Project) -> None:
    project.migration(
        "shop",
        "0001_initial",
        "migrations.RunSQL('SELECT * FROM nowhere'),",
    )
    with pytest.raises(CommandError, match=r"Migrating in shop.0001_initial failed"):
        project.command_db("profile")
    assert connections["default"].settings_dict["NAME"] == ":memory:"


def test_scratch_cleaned_up_on_interrupt(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    with pytest.raises(KeyboardInterrupt):
        project.command_db("profile")
    assert made and not Path(made[0]).exists()
    assert connections["default"].settings_dict["NAME"] == ":memory:"


def test_scratch_unknown_alias() -> None:
    with (
        pytest.raises(ScratchDatabaseError, match="no database alias 'nope'"),
        scratch_database("nope"),
    ):
        pass


def test_hooks_removed(history: Project) -> None:  # noqa: F811
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
