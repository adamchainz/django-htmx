"""Behaviour of the small helper modules, tested directly."""

from __future__ import annotations

import ast
import os
import pickle
import signal
import sys
import warnings
from pathlib import Path
from typing import Any

import pytest
from django.contrib.postgres import operations as pg_operations
from django.db import connections
from django.db.models.signals import post_migrate, pre_migrate
from tokenize_rt import src_to_tokens  # type: ignore[import-untyped]

from django_compact_migrations import isolate, planfile, profile
from django_compact_migrations.core import CompactError
from django_compact_migrations.isolate import ChildError, run_isolated
from django_compact_migrations.noop import (
    Observation,
    classify,
    observe,
    source_concern,
)
from django_compact_migrations.ops import INERT, OPAQUE
from django_compact_migrations.ops import classify as classify_op
from django_compact_migrations.patch import PatchError, apply_diff, make_diff
from django_compact_migrations.runner import RunError, run_history
from django_compact_migrations.scratch import scratch_database
from django_compact_migrations.tokens import container_for_node
from tests import noop_samples as samples
from tests.project import Project
from tests.test_compact import ITEM

# isolate


def test_child_error_pickles() -> None:
    error = pickle.loads(pickle.dumps(ChildError("boom", "trace")))
    assert str(error) == "boom" and error.child_traceback == "trace"


def test_without_fork(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_unpicklable_error_in_child() -> None:
    class Odd(Exception):
        def __init__(self) -> None:
            super().__init__("odd")
            self.callback = lambda: None

    def function(send: Any) -> None:
        raise Odd()

    with pytest.raises(ChildError, match="Odd: odd") as info:
        run_isolated(function)
    assert "Traceback" in info.value.child_traceback


def test_cleanup_failure_in_child_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    parent = os.getpid()

    def close() -> None:
        if os.getpid() != parent:
            raise RuntimeError("can't close")

    monkeypatch.setattr(isolate, "close_everything", close)
    assert run_isolated(lambda send: 42) == 42


def test_child_killed() -> None:
    def function(send: Any) -> None:  # pragma: no cover - its child is killed
        os.kill(os.getpid(), signal.SIGKILL)

    with pytest.raises(ChildError, match="killed by signal 9"):
        run_isolated(function)


def test_parent_interrupted_kills_child() -> None:
    def function(send: Any) -> None:  # pragma: no cover - its child is killed
        send("started")
        signal.pause()

    def receive(message: Any) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_isolated(function, receive)


def test_missing_or_broken_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def flush(self) -> None:
            raise ValueError("closed")

    monkeypatch.setattr(sys, "__stderr__", None)
    monkeypatch.setattr(sys, "stdout", Broken())
    assert run_isolated(lambda send: 1) == 1


def test_kill_gone_child() -> None:
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child exits at once
        os._exit(0)
    os.waitpid(pid, 0)
    isolate._kill(pid)  # neither there to kill nor to wait for


# noop


@pytest.mark.parametrize(
    ("sql", "rowcount", "write"),
    [
        ("-- only a comment", None, False),
        ("SELECT 1; SELECT 2", None, False),
        ("SELECT 1; DELETE FROM t", None, True),
        ("SET CONSTRAINTS ALL IMMEDIATE", None, False),
    ],
)
def test_classify_more(sql: str, rowcount: int | None, write: bool) -> None:
    assert (classify(sql, rowcount) is not None) is write


def test_executemany_update_counts_as_write(django_db_blocker: Any) -> None:
    observation = Observation()
    with django_db_blocker.unblock(), scratch_database() as connection:
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE t (a int)")
        with observe(connection.alias, observation), connection.cursor() as cursor:
            cursor.executemany("UPDATE t SET a = %s", [(1,), (2,)])
    assert observation.writes == ["UPDATE affected unknown rows"]
    assert observation.tables == {"t"}


def test_source_concern_more() -> None:
    assert (
        source_concern(samples._calls_subscript, 0)
        == "it ran no query, yet calls something()"
    )
    assert source_concern(samples._imports_json, 1) is None
    assert source_concern(samples._calls_unreadable, 1) == "its source can't be read"


# ops


def test_postgres_operations() -> None:
    assert classify_op(pg_operations.CreateExtension("hstore")) == INERT
    assert classify_op(pg_operations.HStoreExtension()) == INERT
    assert classify_op(pg_operations.CreateCollation("c", "und")) == INERT
    assert classify_op(pg_operations.CollationOperation("c", "und")) == OPAQUE


# patch


def test_patch_hunks_out_of_order() -> None:
    old = "".join(f"{i}\n" for i in range(20))
    new = old.replace("1\n", "one\n", 1).replace("18\n", "eighteen\n")
    diff = make_diff(old, new, "f")
    header, first, second = diff.split("@@ -", 2)
    swapped = header + "@@ -" + second + "@@ -" + first
    with pytest.raises(PatchError, match="out of order"):
        apply_diff(old, swapped)


# planfile


def test_tool_version_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib.metadata import PackageNotFoundError

    def missing(name: str) -> str:
        raise PackageNotFoundError(name)

    monkeypatch.setattr(planfile, "version", missing)
    assert planfile.tool_version() == "unknown"


def test_module_file() -> None:
    assert planfile._module_file("no.such.module") is None
    assert planfile._module_file("nosuchtoplevelmodule") is None
    assert planfile._module_file("") is None


def test_plan_file_with_missing_migration(project: Project, tmp_path: Path) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "legacy", models.IntegerField(default=0)),',
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )
    plan_path = tmp_path / "plan.toml"
    project.command("plan", "-o", str(plan_path))
    project.path("shop", "0002_add").unlink()
    project.purge()
    with pytest.raises(CompactError, match="can't be read"):
        planfile.load_plan(plan_path)


# profile and runner


def test_profile_without_plan(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.activate()

    def fail(*args: Any, **kwargs: Any) -> None:
        raise CompactError("no plan")

    monkeypatch.setattr(profile, "plan", fail)
    with project.db_blocker.unblock():
        result = profile.profile()
    assert result.removable is None
    assert result.plan_error == "no plan"


def test_run_history_needs_empty_database(project: Project) -> None:
    project.migration("shop", "0001_initial", ITEM)
    project.activate()
    with project.db_blocker.unblock(), scratch_database() as connection:
        run_history(connection)
        with pytest.raises(RunError, match="already has applied migrations"):
            run_history(connection)


def test_shared_operation_timed_once(project: Project) -> None:
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


def test_signal_handler_failures(project: Project) -> None:
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
                run_history(connection, post_migrate=True)
        finally:
            signal_.disconnect(dispatch_uid="test_signal_handler_failures")
    assert connections["default"].vendor == "sqlite"


# tokens


def _container(source: str) -> Any:
    tree = ast.parse(source)
    tokens = list(src_to_tokens(source))
    index = {
        (t.line, t.utf8_byte_offset): i
        for i, t in enumerate(tokens)
        if t.line is not None
    }
    node = tree.body[0].value  # type: ignore[attr-defined]
    return container_for_node(tokens, index, node)


@pytest.mark.parametrize(
    ("source", "found"),
    [
        ("x = [1, 2]", True),
        ("x = 1, 2", False),  # no brackets to edit
        ("x = (1), 2", False),  # the first bracket isn't the tuple's
        ("x = {**a, 'b': 1}", False),
        ("x = [lambda a, b: 1, 2]", False),  # commas inside an element
    ],
)
def test_containers(source: str, found: bool) -> None:
    assert (_container(source) is not None) is found


def test_project_helper(tmp_path: Path, django_db_blocker: Any) -> None:
    project = Project(tmp_path, django_db_blocker)
    project.migration("shop", "0001_initial", ITEM)
    project.activate()
    # A new app deactivates the project, to be activated with it.
    project.migration("other", "0001_initial", "")
    assert project._stack is None
    project.close()
    project.close()
