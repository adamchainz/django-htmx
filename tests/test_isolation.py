"""Scratch runs: every alias retargeted, forked runs, nondeterminism, and the
command-line and plan-file fixes that came with them."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import pytest
from django.db.models.signals import pre_migrate
from django.db.utils import ConnectionHandler

from django_clean_migrations import scratch as scratch_module
from django_clean_migrations.core import Analysis, CleanError
from django_clean_migrations.isolate import ChildError, run_isolated
from django_clean_migrations.runner import RunError
from tests.project import Project
from tests.test_clean import ITEM, kinds
from tests.test_trial import history

# Scratch databases


def test_every_alias_retargeted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, django_db_blocker: Any
) -> None:
    with django_db_blocker.unblock():
        every_alias_retargeted(tmp_path, monkeypatch)


def every_alias_retargeted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sqlite = "django.db.backends.sqlite3"
    primary, other = str(tmp_path / "primary.db"), str(tmp_path / "other.db")
    handler = ConnectionHandler(
        {
            "default": {"ENGINE": sqlite, "NAME": primary},
            "replica": {"ENGINE": sqlite, "NAME": primary},
            "mirror": {"ENGINE": sqlite, "NAME": other, "TEST": {"MIRROR": "default"}},
            "other": {"ENGINE": sqlite, "NAME": other},
            "nowhere": {"ENGINE": "django.db.backends.dummy"},
        }
    )
    monkeypatch.setattr(scratch_module, "connections", handler)
    with scratch_module.scratch_database("default") as connection:
        names = {alias: handler[alias].settings_dict["NAME"] for alias in handler}
        assert connection.settings_dict["NAME"] == names["default"]
        # The replica and the mirror see the primary's scratch database.
        assert names["replica"] == names["mirror"] == names["default"]
        # Another database gets its own, empty one.
        assert names["other"] not in (other, names["default"])
        assert primary not in names.values()
        with handler["replica"].cursor() as cursor:
            cursor.execute("CREATE TABLE t (x int)")
        with handler["default"].cursor() as cursor:
            cursor.execute("SELECT * FROM t")
    assert handler["default"].settings_dict["NAME"] == primary
    assert handler["other"].settings_dict["NAME"] == other
    assert handler["replica"].connection is None
    assert not Path(names["default"]).exists()
    assert not Path(names["other"]).exists()
    assert not Path(primary).exists()


def test_unsupported_alias_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = ConnectionHandler(
        {
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "x"},
            "odd": {"ENGINE": "django.db.backends.sqlite3", "NAME": "y"},
        }
    )
    handler["odd"].vendor = "oracle"
    monkeypatch.setattr(scratch_module, "connections", handler)
    with (
        pytest.raises(scratch_module.ScratchDatabaseError, match="'odd'"),
        scratch_module.scratch_database("default"),
    ):
        pass


# Forked runs


def test_run_isolated() -> None:
    parent = os.getpid()
    messages: list[Any] = []

    def child(send: Any) -> int:
        send("hello")
        return os.getpid()

    assert run_isolated(child, messages.append) != parent
    assert messages == ["hello"]

    def failing(send: Any) -> None:
        raise RunError("boom", ("shop", "0001_initial"))

    with pytest.raises(RunError, match="boom") as info:
        run_isolated(failing)
    assert info.value.migration == ("shop", "0001_initial")

    def crashing(send: Any) -> None:
        os._exit(3)

    with pytest.raises(ChildError, match="exit status 3"):
        run_isolated(crashing)


LEAKY = """
COUNTS = {}


def forwards(apps, schema_editor):
    # A module-level cache, as projects do: wrong if it survives a run.
    COUNTS["runs"] = COUNTS.get("runs", 0) + 1
    if COUNTS["runs"] > 1:
        raise RuntimeError("state leaked from a previous run")
    alias = schema_editor.connection.alias
    apps.get_model("shop", "Item").objects.using(alias).filter(name="legacy").count()
"""


def test_runs_isolated_from_each_other(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "legacy", models.IntegerField(default=0)),\n'
        "migrations.RunPython(forwards),",
        extra=LEAKY,
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )
    result = project.check_equivalence(trial=True)
    assert kinds(result) == [("field", "shop.Item.legacy")]
    assert result.verification["failures"] == []
    assert result.verification["runs"] == 3


def test_pre_migrate_sent(project: Project) -> None:
    def create_table(using: str, **kwargs: Any) -> None:
        from django.db import connections

        with connections[using].cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS aux (x int)")

    pre_migrate.connect(create_table, dispatch_uid="test_pre_migrate_sent")
    try:
        history(
            project,
            "pass",
            extra_ops='migrations.RunSQL("INSERT INTO aux VALUES (1)"),',
        )
        # pre_migrate is only sent for apps with a models module.
        (project.path("shop").parent / "models.py").write_text("")
        result = project.clean(trial=True)
    finally:
        pre_migrate.disconnect(dispatch_uid="test_pre_migrate_sent")
    assert result.verification["failures"] == []


# Nondeterminism and the static plan


def test_nondeterministic_rows_not_blamed(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_seed",
        """
        migrations.AddField("item", "legacy", models.IntegerField(default=0)),
        migrations.RunPython(seed),
        """,
        extra="""
        import uuid


        def seed(apps, schema_editor):
            alias = schema_editor.connection.alias
            Item = apps.get_model("shop", "Item")
            Item.objects.using(alias).create(name=uuid.uuid4().hex)
            Item.objects.using(alias).filter(name="legacy").update(name="x")
        """,
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )
    result = project.check_equivalence(trial=True)
    assert kinds(result) == [("field", "shop.Item.legacy")]
    assert result.verification["nondeterministic_columns"] == {"shop_item": ["name"]}
    assert result.verification["trial_blocked"] == 0


def test_never_worse_than_static(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    history(project, "Item.objects.using(alias).filter(legacy=1).count()")
    project.migration(
        "shop",
        "0004_spare",
        """
        migrations.AddField("item", "spare", models.IntegerField(default=0)),
        migrations.RemoveField("item", "spare"),
        """,
    )
    static = project.clean()
    assert ("field", "shop.Item.spare") in kinds(static)
    original = Analysis.solve

    def solve(self: Any, aggressive: bool = False, **kwargs: Any) -> Any:
        # Simulate blame landing on the static plan's removals.
        if kwargs.get("trial_blocked") is not None:
            static_keys = original(self).unit_keys()
            kwargs["trial_blocked"] = dict(kwargs["trial_blocked"]) | dict.fromkeys(
                static_keys, "test"
            )
        return original(self, aggressive=aggressive, **kwargs)

    monkeypatch.setattr(Analysis, "solve", solve)
    result = project.clean(trial=True)
    # Candidates are solved on top of the static plan: whatever a trial
    # blocks, the static plan's removals stay.
    assert set(kinds(static)) <= set(kinds(result))


def test_baseline_error_explains(project: Project) -> None:
    project.migration(
        "shop", "0001_initial", "migrations.RunSQL('SELECT * FROM nope'),"
    )
    with pytest.raises(CleanError, match="another database alias") as info:
        project.clean(trial=True)
    assert "nope" in str(info.value)


# Command line and plan files


def test_skip_checks_after_subcommand(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    out, _ = project.command("plan", "--skip-checks", "-v", "0")
    assert out == ""


def test_apply_plan_from_elsewhere(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    history(project, "pass")
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.chdir(plans)
    project.command("plan", "-o", "plan.toml")
    data = tomllib.loads((plans / "plan.toml").read_text())
    assert all(f["path"].startswith("../") for f in data["files"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    project.command("apply", "--plan", str(plans / "plan.toml"))
    assert 'AddField("item", "legacy"' not in project.read("shop", "0002_add")
