"""
Run a migration history for real on a (scratch) database, with timings and
hooks around each operation, optionally with substituted operation lists.

Operations are hooked by setting ``database_forwards`` on the instance and
removing it afterwards: their class, and so every isinstance() check and
attribute Django reads (``atomic``, ``reduces_to_sql``, ``elidable``...),
stay untouched.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.migration import Migration
from django.db.migrations.operations.base import Operation

from django_clean_migrations.history import Key, generate_plan

DatabaseForwards = Callable[..., Any]
OpHook = Callable[[Key, int, Operation, DatabaseForwards], DatabaseForwards]
"""Given (migration, index, operation, database_forwards), return a wrapper."""

Progress = Callable[[str, Key | None], None]
"""Called with ("migration", key) before each migration, ("done", None) at the end."""


class RunError(Exception):
    def __init__(self, message: str, migration: Key | None = None) -> None:
        super().__init__(message)
        self.migration = migration


@dataclass(frozen=True)
class OpTiming:
    migration: Key
    index: int
    operation: str
    seconds: float


@dataclass
class RunReport:
    plan: list[Key] = field(default_factory=list)
    migrations: dict[Key, float] = field(default_factory=dict)
    """Wall time per migration, from start to recorded as applied."""
    operations: list[OpTiming] = field(default_factory=list)
    """database_forwards time per operation, including any state rendering."""
    render: float = 0.0
    """Time spent pre-rendering model states before the first migration."""
    total: float = 0.0


def run_history(
    connection: BaseDatabaseWrapper,
    *,
    operations: Mapping[Key, Sequence[Operation]] | None = None,
    hook: OpHook | None = None,
    progress: Progress | None = None,
) -> RunReport:
    """
    Apply every migration to ``connection``, which must be empty, timing each
    migration and operation. ``operations`` replaces the operation lists of
    some migrations, on a private loader only. Failures raise RunError naming
    the migration.
    """
    report = RunReport()
    current: list[Key | None] = [None]
    started: list[float] = [0.0]
    render_started: list[float] = [0.0]

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
                progress("migration", key)
        elif action == "apply_success" and migration is not None:
            key = (migration.app_label, migration.name)
            report.migrations[key] = now - started[0]
            current[0] = None

    executor = MigrationExecutor(connection, progress_callback=cast(Any, callback))
    graph = executor.loader.graph
    if executor.loader.applied_migrations:
        raise RunError("The scratch database already has applied migrations.")
    report.plan = generate_plan(graph)
    for key, ops in (operations or {}).items():
        # The loader made these Migration instances; the shared class-level
        # operations list is not touched.
        graph.nodes[key].operations = list(ops)

    patched: list[Operation] = []
    seen: set[int] = set()
    for key in report.plan:
        for index, op in enumerate(graph.nodes[key].operations):
            if id(op) in seen:
                continue
            seen.add(id(op))
            original = op.database_forwards
            timed = _timed(report, key, index, op, original)
            op.database_forwards = hook(key, index, op, timed) if hook else timed
            patched.append(op)

    begin = time.perf_counter()
    try:
        targets = graph.leaf_nodes()
        executor.migrate(targets, plan=executor.migration_plan(targets))
    except RunError:
        raise
    except Exception as exc:
        failed = current[0]
        where = f" in {failed[0]}.{failed[1]}" if failed else ""
        raise RunError(
            f"Migrating{where} failed: {type(exc).__name__}: {exc}", failed
        ) from exc
    finally:
        report.total = time.perf_counter() - begin
        for op in patched:
            op.__dict__.pop("database_forwards", None)
        if progress is not None:
            progress("done", None)
    return report


def _timed(
    report: RunReport,
    key: Key,
    index: int,
    op: Operation,
    original: DatabaseForwards,
) -> DatabaseForwards:
    def database_forwards(*args: Any, **kwargs: Any) -> Any:
        begin = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            report.operations.append(
                OpTiming(key, index, type(op).__name__, time.perf_counter() - begin)
            )

    return database_forwards
