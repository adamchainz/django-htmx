"""
Run a migration history for real on a (scratch) database, with timings and
hooks around each operation, optionally with substituted operation lists.

Operations are hooked by setting ``database_forwards`` on the instance and
removing it afterwards: their class, and so every isinstance() check and
attribute Django reads (``atomic``, ``reduces_to_sql``, ``elidable``...),
stay untouched.
"""

from __future__ import annotations

import io
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from django.core.management.sql import (
    emit_post_migrate_signal,
    emit_pre_migrate_signal,
)
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.migration import Migration
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

from django_clean_migrations.history import Key, generate_plan
from django_clean_migrations.ops import isolated
from django_clean_migrations.prune import PrunePlan, pruned_graph

DatabaseForwards = Callable[..., Any]
OpHook = Callable[[Key, int, Operation, DatabaseForwards], DatabaseForwards]
"""Given (migration, index, operation, database_forwards), return a wrapper."""

Progress = Callable[[str, Key | None, int, int], None]
"""Called with ("migration", key, done, total) before each migration and
("done", None, done, total) at the end."""


class RunError(Exception):
    def __init__(self, message: str, migration: Key | None = None) -> None:
        super().__init__(message)
        self.migration = migration

    def __reduce__(self) -> Any:
        return (RunError, (str(self), self.migration))


@dataclass
class OpTiming:
    migration: Key
    index: int
    operation: str
    seconds: float = 0.0
    """In database_forwards: the schema change or data migration itself,
    including any model rendering it triggers."""
    state_seconds: float = 0.0
    """Updating the project state for it: Migration.apply()'s clone of the
    state before the operation, plus state_forwards() (which re-renders the
    affected models once they have been rendered)."""

    @property
    def total(self) -> float:
        return self.seconds + self.state_seconds


@dataclass
class RunReport:
    plan: list[Key] = field(default_factory=list)
    migrations: dict[Key, float] = field(default_factory=dict)
    """Wall time per migration, from start to recorded as applied."""
    operations: list[OpTiming] = field(default_factory=list)
    render: float = 0.0
    """Time spent pre-rendering model states before the first migration."""
    total: float = 0.0


def run_history(
    connection: BaseDatabaseWrapper,
    *,
    operations: Mapping[Key, Sequence[Operation]] | None = None,
    hook: OpHook | None = None,
    progress: Progress | None = None,
    prune: PrunePlan | None = None,
    post_migrate: bool = False,
) -> RunReport:
    """
    Apply every migration to ``connection``, which must be empty, timing each
    migration and operation. ``operations`` replaces the operation lists of
    some migrations, on a private loader only; ``prune`` deletes migrations
    from its graph. Failures raise RunError naming
    the migration.

    pre_migrate is sent first, as ``migrate`` does. post_migrate only with
    ``post_migrate``: it runs after the history, so it can't change how the
    history migrates, but what its handlers create (content types and
    permissions, numbered in order) shows the effect of rows the history
    created first.
    """
    report = RunReport()
    current: list[Key | None] = [None]
    started: list[float] = [0.0]
    render_started: list[float] = [0.0]
    done = [0]

    def callback(
        action: str, migration: Migration | None = None, fake: bool = False
    ) -> None:
        now = time.perf_counter()
        if action == "render_start":
            render_started[0] = now
        elif action == "render_success":
            report.render += now - render_started[0]
        elif action == "apply_start" and migration is not None:
            key = (migration.app_label, migration.name)
            current[0] = key
            started[0] = now
            if progress is not None:
                progress("migration", key, done[0], len(report.plan))
        elif action == "apply_success" and migration is not None:
            key = (migration.app_label, migration.name)
            report.migrations[key] = now - started[0]
            current[0] = None
            done[0] += 1

    executor = MigrationExecutor(connection, progress_callback=cast(Any, callback))
    if prune:
        # Pruned migrations deleted, their dependants rewired.
        executor.loader.graph = pruned_graph(executor.loader.graph, prune)
    graph = executor.loader.graph
    if executor.loader.applied_migrations:
        raise RunError("The scratch database already has applied migrations.")
    report.plan = generate_plan(graph)
    replaced = operations or {}
    for key in report.plan:
        # The loader made these Migration instances; the shared class-level
        # operations list (and, via isolated(), its field objects) is not
        # touched.
        migration = graph.nodes[key]
        migration.operations = [
            isolated(op) for op in replaced.get(key, migration.operations)
        ]

    patched: list[Operation] = []
    seen: set[int] = set()
    clone_time = [0.0]
    for key in report.plan:
        for index, op in enumerate(graph.nodes[key].operations):
            if id(op) in seen:
                continue
            seen.add(id(op))
            timing = OpTiming(key, index, type(op).__name__)
            report.operations.append(timing)
            timed = _timed_db(timing, op.database_forwards)
            op.database_forwards = hook(key, index, op, timed) if hook else timed
            op.state_forwards = _timed_state(timing, op.state_forwards, clone_time)
            patched.append(op)

    original_clone = ProjectState.clone

    def clone(self: ProjectState) -> ProjectState:
        # Migration.apply() clones the state before each operation; charged
        # to the operation whose state_forwards() comes next.
        begin = time.perf_counter()
        try:
            return original_clone(self)
        finally:
            clone_time[0] += time.perf_counter() - begin

    targets = graph.leaf_nodes()
    migration_plan = executor.migration_plan(targets)
    try:
        # As migrate does: handlers may prepare the database (create a
        # schema, an extension...) or, like contenttypes, add operations.
        emit_pre_migrate_signal(
            0,
            False,
            connection.alias,
            stdout=io.StringIO(),
            apps=cast(Any, executor)
            ._create_project_state(with_applied_migrations=True)
            .apps,
            plan=migration_plan,
        )
    except Exception as exc:
        raise RunError(
            f"A pre_migrate signal handler failed: {type(exc).__name__}: {exc}"
        ) from exc
    begin = time.perf_counter()
    ProjectState.clone = clone  # type: ignore[method-assign]
    try:
        final_state = executor.migrate(targets, plan=migration_plan)
    except RunError:
        raise
    except Exception as exc:
        failed = current[0]
        where = f" in {failed[0]}.{failed[1]}" if failed else ""
        raise RunError(
            f"Migrating{where} failed: {type(exc).__name__}: {exc}", failed
        ) from exc
    finally:
        ProjectState.clone = original_clone  # type: ignore[method-assign]
        report.total = time.perf_counter() - begin
        for op in patched:
            op.__dict__.pop("database_forwards", None)
            op.__dict__.pop("state_forwards", None)
        if progress is not None:
            progress("done", None, done[0], len(report.plan))
    if post_migrate:
        try:
            emit_post_migrate_signal(
                0,
                False,
                connection.alias,
                stdout=io.StringIO(),
                apps=final_state.apps,
                plan=migration_plan,
            )
        except Exception as exc:
            raise RunError(
                f"A post_migrate signal handler failed: {type(exc).__name__}: {exc}"
            ) from exc
    return report


def _timed_db(timing: OpTiming, original: DatabaseForwards) -> DatabaseForwards:
    def database_forwards(*args: Any, **kwargs: Any) -> Any:
        begin = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            timing.seconds += time.perf_counter() - begin

    return database_forwards


def _timed_state(
    timing: OpTiming, original: Callable[..., Any], clone_time: list[float]
) -> Callable[..., Any]:
    def state_forwards(*args: Any, **kwargs: Any) -> Any:
        timing.state_seconds += clone_time[0]
        clone_time[0] = 0.0
        begin = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            timing.state_seconds += time.perf_counter() - begin

    return state_forwards
