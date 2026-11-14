"""Run the original history on a scratch database and see where time goes."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from django_clean_migrations.core import CleanError, Result, plan
from django_clean_migrations.history import Key
from django_clean_migrations.runner import Progress, RunError, run_history
from django_clean_migrations.scratch import ScratchDatabaseError, scratch_database


@dataclass
class TypeStats:
    count: int = 0
    seconds: float = 0.0


@dataclass
class Profile:
    vendor: str
    total: float
    """Wall time of the whole migrate run."""
    operations_total: float
    """Time inside operations' database_forwards (including state rendering)."""
    render: float
    migrations: dict[Key, float]
    migration_ops: dict[Key, int]
    by_type: dict[str, TypeStats]
    by_app: dict[str, float]
    removable: float | None
    """Time in operations the default plan removes outright (None: no plan)."""
    removable_count: int
    editable: float | None
    """Time in operations the default plan edits."""
    plan_error: str | None = None
    notes: list[str] = field(default_factory=list)

    def slowest(self, n: int) -> list[tuple[Key, float]]:
        return sorted(self.migrations.items(), key=lambda kv: -kv[1])[:n]

    def as_dict(self, top: int) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "total_seconds": self.total,
            "operations_seconds": self.operations_total,
            "render_seconds": self.render,
            "migrations": len(self.migrations),
            "slowest_migrations": [
                {
                    "migration": f"{k[0]}.{k[1]}",
                    "seconds": s,
                    "operations": self.migration_ops.get(k, 0),
                }
                for k, s in self.slowest(top)
            ],
            "by_operation_type": {
                t: {"count": v.count, "seconds": v.seconds}
                for t, v in sorted(self.by_type.items(), key=lambda kv: -kv[1].seconds)
            },
            "by_app": dict(sorted(self.by_app.items(), key=lambda kv: -kv[1])),
            "removable_seconds": self.removable,
            "removable_operations": self.removable_count,
            "editable_seconds": self.editable,
            "plan_error": self.plan_error,
            "notes": self.notes,
        }


def profile(alias: str = "default", progress: Progress | None = None) -> Profile:
    """
    Migrate a scratch database from scratch, timing every migration and
    operation. post_migrate signals (content types, permissions) are not
    sent, as ``migrate`` would at the end: they are outside the history.
    """
    try:
        static: Result | None = plan()
        plan_error = None
    except CleanError as exc:
        static, plan_error = None, str(exc)
    try:
        with scratch_database(alias) as connection:
            vendor = connection.vendor
            report = run_history(connection, progress=progress)
    except (ScratchDatabaseError, RunError) as exc:
        raise CleanError(str(exc)) from exc

    by_type: dict[str, TypeStats] = defaultdict(TypeStats)
    migration_ops: dict[Key, int] = defaultdict(int)
    removable = 0.0
    editable = 0.0
    removable_count = 0
    for t in report.operations:
        stats = by_type[t.operation]
        stats.count += 1
        stats.seconds += t.seconds
        migration_ops[t.migration] += 1
        if static is not None:
            if (t.migration, t.index) in static.removed_positions:
                removable += t.seconds
                removable_count += 1
            elif (t.migration, t.index) in static.edited_positions:
                editable += t.seconds
    by_app: dict[str, float] = defaultdict(float)
    for key, seconds in report.migrations.items():
        by_app[key[0]] += seconds
    return Profile(
        vendor=vendor,
        total=report.total,
        operations_total=sum(t.seconds for t in report.operations),
        render=report.render,
        migrations=report.migrations,
        migration_ops=dict(migration_ops),
        by_type=dict(by_type),
        by_app=dict(by_app),
        removable=removable if static is not None else None,
        removable_count=removable_count,
        editable=editable if static is not None else None,
        plan_error=plan_error,
        notes=["post_migrate signals (content types, permissions) are not run."],
    )
