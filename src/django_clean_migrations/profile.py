"""Run the original history on a scratch database and see where time goes."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from django_clean_migrations.core import CleanError, Result, plan
from django_clean_migrations.history import Key
from django_clean_migrations.isolate import ChildError, Send, run_isolated
from django_clean_migrations.runner import (
    OpTiming,
    Progress,
    RunError,
    RunReport,
    run_history,
)
from django_clean_migrations.scratch import ScratchDatabaseError, scratch_database


@dataclass
class TypeStats:
    count: int = 0
    seconds: float = 0.0
    """In the database (database_forwards)."""
    state_seconds: float = 0.0
    """Updating the project state (state clone and state_forwards)."""

    @property
    def total(self) -> float:
        return self.seconds + self.state_seconds


@dataclass
class Profile:
    vendor: str
    total: float
    """Wall time of the whole migrate run."""
    operations_total: float
    """Time inside operations' database_forwards (including model rendering
    they trigger)."""
    state_total: float
    """Time updating the project state, per operation."""
    render: float
    migrations: dict[Key, float]
    migration_ops: dict[Key, int]
    by_type: dict[str, TypeStats]
    by_app: dict[str, TypeStats]
    by_migration: dict[Key, TypeStats]
    top_state: list[OpTiming]
    removable: float | None
    """Database + state time of operations the default plan removes or
    folds away (None: no plan)."""
    removable_count: int
    editable: float | None
    """Database + state time of operations the default plan edits."""
    plan_error: str | None = None
    notes: list[str] = field(default_factory=list)
    server: str = ""
    """The server's name ("MariaDB" for Django's "mysql" vendor)."""

    @property
    def overhead(self) -> float:
        """Executor time outside operations: recording, transactions,
        deferred SQL, the initial render."""
        return max(0.0, self.total - self.operations_total - self.state_total)

    def slowest(self, n: int) -> list[tuple[Key, float]]:
        return sorted(self.migrations.items(), key=lambda kv: -kv[1])[:n]

    def as_dict(self, top: int) -> dict[str, Any]:
        def stats(v: TypeStats) -> dict[str, Any]:
            return {
                "count": v.count,
                "database_seconds": v.seconds,
                "state_seconds": v.state_seconds,
            }

        return {
            "vendor": self.vendor,
            "server": self.server,
            "total_seconds": self.total,
            "database_seconds": self.operations_total,
            "state_seconds": self.state_total,
            "overhead_seconds": self.overhead,
            "render_seconds": self.render,
            "migrations": len(self.migrations),
            "slowest_migrations": [
                {
                    "migration": f"{k[0]}.{k[1]}",
                    "seconds": s,
                    "operations": self.migration_ops.get(k, 0),
                    **stats(self.by_migration.get(k, TypeStats())),
                }
                for k, s in self.slowest(top)
            ],
            "by_operation_type": {
                t: stats(v)
                for t, v in sorted(self.by_type.items(), key=lambda kv: -kv[1].total)
            },
            "by_app": {
                a: stats(v)
                for a, v in sorted(self.by_app.items(), key=lambda kv: -kv[1].total)
            },
            "top_state_operations": [
                {
                    "migration": f"{t.migration[0]}.{t.migration[1]}",
                    "index": t.index,
                    "operation": t.operation,
                    "state_seconds": t.state_seconds,
                }
                for t in self.top_state[:top]
            ],
            "removable_seconds": self.removable,
            "removable_operations": self.removable_count,
            "editable_seconds": self.editable,
            "plan_error": self.plan_error,
            "notes": self.notes,
        }


def profile(alias: str = "default", progress: Progress | None = None) -> Profile:
    """
    Migrate a scratch database from scratch, timing every migration and
    operation, in a forked child process. pre_migrate is sent, as ``migrate``
    does; post_migrate (content types, permissions) isn't: it is outside the
    history.
    """
    try:
        static: Result | None = plan()
        plan_error = None
    except CleanError as exc:
        static, plan_error = None, str(exc)
    try:
        with scratch_database(alias) as connection:
            vendor, server = connection.vendor, connection.display_name

            def child(send: Send) -> RunReport:
                def forward(event: str, key: Key | None, done: int, total: int) -> None:
                    send((event, key, done, total))

                return run_history(connection, progress=forward)

            report = run_isolated(
                child, (lambda message: progress(*message)) if progress else None
            )
    except (ScratchDatabaseError, RunError, ChildError) as exc:
        raise CleanError(str(exc)) from exc

    by_type: dict[str, TypeStats] = defaultdict(TypeStats)
    by_app: dict[str, TypeStats] = defaultdict(TypeStats)
    by_migration: dict[Key, TypeStats] = defaultdict(TypeStats)
    migration_ops: dict[Key, int] = defaultdict(int)
    removable = 0.0
    editable = 0.0
    removable_count = 0
    for t in report.operations:
        for stats in (
            by_type[t.operation],
            by_app[t.migration[0]],
            by_migration[t.migration],
        ):
            stats.count += 1
            stats.seconds += t.seconds
            stats.state_seconds += t.state_seconds
        migration_ops[t.migration] += 1
        if static is not None:
            if (t.migration, t.index) in static.removed_positions:
                removable += t.total
                removable_count += 1
            elif (t.migration, t.index) in static.edited_positions:
                editable += t.total
    return Profile(
        vendor=vendor,
        server=server,
        total=report.total,
        operations_total=sum(t.seconds for t in report.operations),
        state_total=sum(t.state_seconds for t in report.operations),
        render=report.render,
        migrations=report.migrations,
        migration_ops=dict(migration_ops),
        by_type=dict(by_type),
        by_app=dict(by_app),
        by_migration=dict(by_migration),
        top_state=sorted(report.operations, key=lambda t: -t.state_seconds),
        removable=removable if static is not None else None,
        removable_count=removable_count,
        editable=editable if static is not None else None,
        plan_error=plan_error,
        notes=["post_migrate signals (content types, permissions) are not run."],
    )
