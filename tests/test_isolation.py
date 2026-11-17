"""Scratch runs: every alias retargeted, forked runs, the isolate module,
nondeterminism, and how the command reports them."""

from __future__ import annotations

import os
import pickle
import signal
import subprocess
import sys
import tomllib
import warnings
from pathlib import Path
from typing import Any

import pytest
from django.db import connections
from django.db.models.signals import pre_migrate
from django.db.utils import ConnectionHandler

from django_compact_migrations import isolate
from django_compact_migrations import scratch as scratch_module
from django_compact_migrations.core import Analysis, CompactError
from django_compact_migrations.isolate import ChildError, run_isolated
from django_compact_migrations.runner import RunError
from tests.project import ITEM, kinds, with_data_migration

# Scratch databases


def test_every_alias_retargeted(tmp_path, monkeypatch, django_db_blocker):
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


def test_unsupported_alias_refused(tmp_path, monkeypatch):
    handler = ConnectionHandler(
        {
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "x"},
            "odd": {"ENGINE": "django.db.backends.sqlite3", "NAME": "y"},
        }
    )
    handler["odd"].vendor = "oracle"
    monkeypatch.setattr(scratch_module, "connections", handler)
    with pytest.raises(scratch_module.ScratchDatabaseError, match="'odd'"):
        scratch_module.scratch_database("default").__enter__()


# Forked runs


def test_run_isolated():
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


def test_runs_isolated_from_each_other(project):
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
    result = project.check_equivalence(static=False)
    # The static plan folds the AddField (its removal is blocked by the
    # mention); the trial then removes the field too.
    assert kinds(result) == [
        ("field", "shop.Item.legacy"),
        ("fold", "shop.Item.legacy"),
    ]
    assert result.verification["failures"] == []
    assert result.verification["runs"] == 3


def test_pre_migrate_sent(project):
    def create_table(using: str, **kwargs: Any) -> None:

        with connections[using].cursor() as cursor:
            cursor.execute("CREATE TABLE IF NOT EXISTS aux (x int)")

    pre_migrate.connect(create_table, dispatch_uid="test_pre_migrate_sent")
    try:
        with_data_migration(
            project,
            "pass",
            extra_ops='migrations.RunSQL("INSERT INTO aux VALUES (1)"),',
        )
        # pre_migrate is only sent for apps with a models module.
        (project.path("shop").parent / "models.py").write_text("")
        result = project.compact(static=False)
    finally:
        pre_migrate.disconnect(dispatch_uid="test_pre_migrate_sent")
    assert result.verification["failures"] == []


# Nondeterminism and the static plan


def test_nondeterministic_rows_not_blamed(project):
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
    result = project.check_equivalence(static=False)
    assert kinds(result) == [
        ("field", "shop.Item.legacy"),
        ("fold", "shop.Item.legacy"),
    ]
    assert result.verification["nondeterministic_columns"] == {"shop_item": ["name"]}
    assert result.verification["trial_blocked"] == 0


def test_never_worse_than_static(project, monkeypatch):
    with_data_migration(project, "Item.objects.using(alias).filter(legacy=1).count()")
    project.migration(
        "shop",
        "0004_spare",
        """
        migrations.AddField("item", "spare", models.IntegerField(default=0)),
        migrations.RemoveField("item", "spare"),
        """,
    )
    static = project.compact()
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
    result = project.compact(static=False)
    # Candidates are solved on top of the static plan: whatever a trial
    # blocks, the static plan's removals stay.
    assert set(kinds(static)) <= set(kinds(result))


def test_baseline_error_explains(project):
    project.migration(
        "shop", "0001_initial", "migrations.RunSQL('SELECT * FROM nope'),"
    )
    with pytest.raises(CompactError, match="another database alias") as info:
        project.compact(static=False)
    assert "nope" in str(info.value)


# Command line and plan files


def test_skip_checks_after_subcommand(project):
    project.migration("shop", "0001_initial", ITEM)
    out, _ = project.command("plan", "--static", "--skip-checks", "-v", "0")
    assert out == ""


def test_apply_plan_from_elsewhere(project, tmp_path, monkeypatch):
    # The project root is the Git work tree, from any directory in it.
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    with_data_migration(project, "pass")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    plans = tmp_path / "plans"
    plans.mkdir()
    monkeypatch.chdir(plans)
    project.command("plan", "--static", "-o", "plan.toml")
    data = tomllib.loads((plans / "plan.toml").read_text())
    assert all(f["path"].startswith("../") for f in data["files"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    project.command("apply", "--plan", str(plans / "plan.toml"))
    assert 'AddField("item", "legacy"' not in project.read("shop", "0002_add")


def test_child_error_pickles():
    error = pickle.loads(pickle.dumps(ChildError("boom", "trace")))
    assert str(error) == "boom" and error.child_traceback == "trace"


def test_without_fork(monkeypatch):
    monkeypatch.setattr(isolate, "CAN_FORK", False)
    messages: list[Any] = []

    def function(send: Any) -> int:
        send("hi")
        return os.getpid()

    with pytest.warns(RuntimeWarning, match="os.fork"):
        assert run_isolated(function, messages.append) == os.getpid()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert run_isolated(function, messages.append, fork=False) == os.getpid()
    assert messages == ["hi", "hi"]


def test_unpicklable_error_in_child():
    class Odd(Exception):
        def __init__(self) -> None:
            super().__init__("odd")
            self.callback = lambda: None

    def function(send: Any) -> None:
        raise Odd()

    with pytest.raises(ChildError, match="Odd: odd") as info:
        run_isolated(function)
    assert "Traceback" in info.value.child_traceback


def test_cleanup_failure_in_child_ignored(monkeypatch):
    parent = os.getpid()

    def close() -> None:
        if os.getpid() != parent:
            raise RuntimeError("can't close")

    monkeypatch.setattr(isolate, "close_everything", close)
    assert run_isolated(lambda send: 42) == 42


def test_child_killed():
    def function(send: Any) -> None:  # pragma: no cover - its child is killed
        os.kill(os.getpid(), signal.SIGKILL)

    with pytest.raises(ChildError, match="killed by signal 9"):
        run_isolated(function)


def test_parent_interrupted_kills_child():
    def function(send: Any) -> None:  # pragma: no cover - its child is killed
        send("started")
        signal.pause()

    def receive(message: Any) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_isolated(function, receive)


def test_missing_or_broken_stream(monkeypatch):
    class Broken:
        def flush(self) -> None:
            raise ValueError("closed")

    monkeypatch.setattr(sys, "__stderr__", None)
    monkeypatch.setattr(sys, "stdout", Broken())
    assert run_isolated(lambda send: 1) == 1


def test_kill_gone_child():
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child exits at once
        os._exit(0)
    os.waitpid(pid, 0)
    isolate._kill(pid)  # neither there to kill nor to wait for
