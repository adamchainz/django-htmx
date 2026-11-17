"""
Run a migration history for real on a (scratch) database, with hooks around
each operation, optionally with substituted operation lists.

Operations are hooked by setting ``database_forwards`` on the instance and
removing it afterwards: their class, and so every isinstance() check and
attribute Django reads (``atomic``, ``reduces_to_sql``, ``elidable``...),
stay untouched.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

from django.core.management.sql import (
    emit_post_migrate_signal,
    emit_pre_migrate_signal,
)
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.migration import Migration
from django.db.migrations.operations.base import Operation

from django_compact_migrations.history import Key, generate_plan
from django_compact_migrations.ops import isolated
from django_compact_migrations.prune import (
    PruneError,
    PrunePlan,
    app_ends,
    check_ends,
    pruned_graph,
)

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


def run_history(
    connection: BaseDatabaseWrapper,
    *,
    operations: Mapping[Key, Sequence[Operation]] | None = None,
    hook: OpHook | None = None,
    progress: Progress | None = None,
    prune: PrunePlan | None = None,
) -> None:
    """
    Apply every migration to ``connection``, which must be empty.
    ``operations`` replaces the operation lists of
    some migrations, on a private loader only; ``prune`` deletes migrations
    from its graph. Failures raise RunError naming
    the migration.

    pre_migrate is sent first and post_migrate last, as ``migrate`` does:
    post_migrate runs after the history, so it can't change how the history
    migrates, but what its handlers create (content types and permissions,
    numbered in order) shows the effect of rows the history created first.
    """
    current: list[Key | None] = [None]
    done = [0]
    plan: list[Key] = []

    def callback(
        action: str, migration: Migration | None = None, fake: bool = False
    ) -> None:
        if action == "apply_start" and migration is not None:
            key = (migration.app_label, migration.name)
            current[0] = key
            if progress is not None:
                progress("migration", key, done[0], len(plan))
        elif action == "apply_success":  # nothing is ever unapplied
            current[0] = None
            done[0] += 1

    executor = MigrationExecutor(connection, progress_callback=cast(Any, callback))
    if prune:
        # Pruned migrations deleted, their dependants rewired; checked as
        # migrate checks for conflicts (several latest migrations in an app).
        old_graph = executor.loader.graph
        executor.loader.graph = pruned_graph(old_graph, prune)
        try:
            check_ends(app_ends(old_graph), executor.loader.graph, prune.pruned)
        except PruneError as exc:
            raise RunError(f"The pruned migration graph is wrong: {exc}") from exc
    graph = executor.loader.graph
    if executor.loader.applied_migrations:
        raise RunError("The scratch database already has applied migrations.")
    plan.extend(generate_plan(graph))
    replaced = operations or {}
    for key in plan:
        # The loader made these Migration instances; the shared class-level
        # operations list (and, via isolated(), its field objects) is not
        # touched.
        migration = graph.nodes[key]
        migration.operations = [
            isolated(op) for op in replaced.get(key, migration.operations)
        ]

    patched: list[Operation] = []
    seen: set[int] = set()
    if hook is not None:
        for key in plan:
            for index, op in enumerate(graph.nodes[key].operations):
                if id(op) in seen:
                    continue
                seen.add(id(op))
                op.database_forwards = hook(key, index, op, op.database_forwards)
                patched.append(op)

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
    try:
        final_state = executor.migrate(targets, plan=migration_plan)
    except Exception as exc:
        failed = current[0]
        where = f" in {failed[0]}.{failed[1]}" if failed else ""
        raise RunError(
            f"Migrating{where} failed: {type(exc).__name__}: {exc}", failed
        ) from exc
    finally:
        for op in patched:
            op.__dict__.pop("database_forwards", None)
        if progress is not None:
            progress("done", None, done[0], len(plan))
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
