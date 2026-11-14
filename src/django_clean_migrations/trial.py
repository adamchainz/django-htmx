"""
Trial mode: replace the opaque-operation name scan by evidence.

1. Run the original history on a scratch database and snapshot it.
2. Solve aggressively: name-scan and soft-reference hits don't block, but are
   recorded per removal ("at risk").
3. Run the candidate's edited operations (in memory, files untouched) on a
   fresh scratch database. Failures in opaque operations are harvested (rolled
   back to a savepoint, recorded, and skipped) where the backend can roll back
   DDL and the migration is atomic; any other failure ends the pass.
4. Block the removals each failure or snapshot difference points at, solve
   again, and repeat until a pass has no failures and matches the baseline.
   If that doesn't happen within the run limit, fall back to the static plan,
   which must pass the same trial.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.operations.base import Operation

from django_clean_migrations.core import (
    Analysis,
    CleanError,
    Result,
    build,
    make_result,
)
from django_clean_migrations.history import Key, OpRecord
from django_clean_migrations.opaque import OpaqueOp, Text
from django_clean_migrations.ops import OPAQUE, classify
from django_clean_migrations.runner import DatabaseForwards, RunError, run_history
from django_clean_migrations.scratch import ScratchDatabaseError, scratch_database
from django_clean_migrations.snapshot import Difference, Snapshot, take_snapshot
from django_clean_migrations.timeline import Life

if TYPE_CHECKING:
    from django_clean_migrations.solver import Solver, Unit

Progress = Callable[[str], None]

_candidate_hook: Callable[[dict[Key, list[Operation]], int], None] | None = None
"""Test hook: may alter a candidate's operations before pass ``n`` runs."""


@dataclass(frozen=True)
class TrialFailure:
    migration: Key
    operation: str
    error: str
    traceback: str
    harvested: bool
    """True if the run went on past it, False if it ended the pass."""
    seq: int | None = None
    """The failing original operation (OpRecord.seq), if known."""

    def describe(self) -> str:
        return (
            f"trial migrate failed in {self.migration[0]}.{self.migration[1]}"
            f" {self.operation}: {self.error}"
        )


@dataclass
class PassResult:
    failures: list[TrialFailure]
    snapshot: Snapshot | None
    aborted: bool


class Trial:
    def __init__(
        self,
        analysis: Analysis,
        alias: str = "default",
        max_runs: int = 8,
        progress: Progress | None = None,
    ) -> None:
        self.analysis = analysis
        self.alias = alias
        self.max_runs = max(1, max_runs)
        self.progress = progress
        history = analysis.history
        self.records: dict[int, OpRecord] = {id(rec.op): rec for rec in history.ops}
        self.opaque: dict[int, OpaqueOp] = {
            o.rec.seq: o for o in analysis.timeline.opaque
        }
        self.vendor = ""
        self.can_rollback_ddl = False
        self.harvested: list[TrialFailure] = []
        self.masked: set[str] = set()
        self.passes = 0

    # Running

    def _say(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def _run(
        self, label: str, operations: Mapping[Key, Sequence[Operation]] | None
    ) -> PassResult:
        failures: list[TrialFailure] = []
        current: list[tuple[Key, Operation] | None] = [None]
        harvest = operations is not None

        def on_progress(event: str, key: Key | None) -> None:
            if key is not None:
                self._say(f"{label}: migrating {key[0]}.{key[1]}")

        try:
            with scratch_database(self.alias) as connection:
                self.vendor = connection.vendor
                self.can_rollback_ddl = bool(connection.features.can_rollback_ddl)
                hook = self._hook(connection, failures, current, harvest)
                try:
                    run_history(
                        connection,
                        operations=operations,
                        hook=hook,
                        progress=on_progress,
                    )
                except RunError as exc:
                    where = current[0]
                    cause = exc.__cause__ or exc
                    rec = self.records.get(id(where[1])) if where else None
                    failures.append(
                        TrialFailure(
                            migration=exc.migration
                            or (where[0] if where else ("?", "?")),
                            operation=type(where[1]).__name__ if where else "?",
                            error=f"{type(cause).__name__}: {cause}",
                            traceback=_trimmed(cause),
                            harvested=False,
                            seq=rec.seq if rec is not None else None,
                        )
                    )
                    return PassResult(failures, None, True)
                self._say(f"{label}: taking a snapshot")
                return PassResult(failures, take_snapshot(connection), False)
        except ScratchDatabaseError as exc:
            raise CleanError(str(exc)) from exc

    def _hook(
        self,
        connection: BaseDatabaseWrapper,
        failures: list[TrialFailure],
        current: list[tuple[Key, Operation] | None],
        harvest: bool,
    ) -> Callable[[Key, int, Operation, DatabaseForwards], DatabaseForwards]:
        history = self.analysis.history

        def hook(
            key: Key, index: int, op: Operation, forwards: DatabaseForwards
        ) -> DatabaseForwards:
            rec = self.records.get(id(op))
            opaque = self.opaque.get(rec.seq) if rec is not None else None
            # Only ops without state changes can be skipped without the
            # database and the project state drifting apart.
            harvestable = (
                harvest
                and classify(op) == OPAQUE
                and opaque is not None
                and not opaque.state_changed
                and bool(history.by_key[key].migration.atomic)
                and bool(connection.features.can_rollback_ddl)
            )

            def database_forwards(*args: Any, **kwargs: Any) -> Any:
                current[0] = (key, op)
                if not harvestable:
                    return forwards(*args, **kwargs)
                try:
                    with transaction.atomic(using=connection.alias, savepoint=True):
                        return forwards(*args, **kwargs)
                except Exception as exc:
                    failures.append(
                        TrialFailure(
                            migration=key,
                            operation=type(op).__name__,
                            error=f"{type(exc).__name__}: {exc}",
                            traceback=_trimmed(exc),
                            harvested=True,
                            seq=rec.seq if rec is not None else None,
                        )
                    )
                    return None

            return database_forwards

        return hook

    # The loop

    def run(self) -> Result:
        begin = time.perf_counter()
        baseline = self._run("Baseline", None)
        if baseline.aborted or baseline.snapshot is None:
            failure = baseline.failures[0] if baseline.failures else None
            raise CleanError(
                "The original migration history doesn't migrate a fresh"
                f" {self.vendor or 'scratch'} database, so it can't be trialled:"
                f" {failure.error if failure else 'unknown error'}"
                + (
                    f" (in {failure.migration[0]}.{failure.migration[1]})"
                    if failure
                    else ""
                )
            )
        reference = baseline.snapshot
        static_solver = self.analysis.solve()
        static_units = _removed_units(static_solver)
        trial_blocked: dict[Unit, str] = {}
        fallback = False
        while True:
            if self.passes >= self.max_runs:
                fallback = True
                break
            solver = self.analysis.solve(aggressive=True, trial_blocked=trial_blocked)
            built = build(self.analysis, solver)
            outcome = self._candidate(solver, built, reference)
            if outcome is None:
                return self._result(
                    solver, built, static_units, trial_blocked, fallback, begin
                )
            blocks = self._attribute(solver, outcome[0], outcome[1])
            fresh = {u: r for u, r in blocks.items() if u not in trial_blocked}
            if not fresh:
                fallback = True
                break
            trial_blocked.update(fresh)

        solver = self.analysis.solve()
        built = build(self.analysis, solver)
        outcome = self._candidate(solver, built, reference)
        if outcome is not None:
            failures, diffs = outcome
            problems = [f.describe() for f in failures] + [str(d) for d in diffs]
            raise CleanError(
                "Even the static plan failed its trial run, so nothing can be"
                " planned with --trial: " + "; ".join(problems[:5])
            )
        return self._result(solver, built, static_units, trial_blocked, fallback, begin)

    def _candidate(
        self, solver: Solver, built: Any, reference: Snapshot
    ) -> tuple[list[TrialFailure], list[Difference]] | None:
        """Run a candidate; None if it passes, else failures and differences."""
        operations = built.operations_by_key(self.analysis.history)
        if _candidate_hook is not None:
            _candidate_hook(operations, self.passes)
        self.passes += 1
        result = self._run(f"Trial run {self.passes}", operations)
        self.harvested.extend(result.failures)
        if result.failures or result.snapshot is None:
            # Skipped failing operations explain any snapshot differences,
            # so only the failures are attributed this time.
            return result.failures, []
        diffs = reference.differences(result.snapshot, self.masked)
        if not result.failures and diffs:
            diffs = self._mask_nondeterminism(solver, diffs, reference, result.snapshot)
        if not result.failures and not diffs:
            return None
        return result.failures, diffs

    def _mask_nondeterminism(
        self,
        solver: Solver,
        diffs: list[Difference],
        reference: Snapshot,
        snapshot: Snapshot,
    ) -> list[Difference]:
        """
        Data differences in tables no removal relates to, when nothing at risk
        is left to blame, come from the history itself (random or time-based
        values in seeded rows): mask those tables rather than loop.
        """
        at_risk = [u for u in _removed_units(solver) if solver.at_risk.get(u)]
        if at_risk:
            return diffs
        units = _removed_units(solver)
        for d in diffs:
            if d.kind == "data" and d.table and not self._related(units, d.table):
                self.masked.add(d.table)
        return reference.differences(snapshot, self.masked)

    # Attribution

    def _attribute(
        self,
        solver: Solver,
        failures: list[TrialFailure],
        diffs: list[Difference],
    ) -> dict[Unit, str]:
        units = _removed_units(solver)
        blocks: dict[Unit, str] = {}
        history = self.analysis.history
        for failure in failures:
            reason = failure.describe()
            targets: list[Unit] = []
            seq = failure.seq
            if seq is not None and seq in self.opaque:
                targets = [u for u in units if seq in solver.at_risk.get(u, {})]
                # One data migration can be at risk for several removals; when
                # the error names some of them ("Cannot resolve keyword
                # 'legacy'"), only those are to blame.
                named = [u for u in targets if _named_in(u, failure.error)]
                if named:
                    targets = named
            if not targets and failure.migration in history.by_key:
                mig = history.by_key[failure.migration].index
                targets = [u for u in units if self._in_window(solver, u, mig)]
            for u in targets:
                blocks.setdefault(u, reason)
        for d in diffs:
            reason = f"trial: {d}"
            targets = self._related(units, d.table) if d.table else []
            if not targets and d.kind == "data":
                targets = [u for u in units if solver.at_risk.get(u)]
            for u in targets:
                blocks.setdefault(u, reason)
        return blocks

    def _in_window(self, solver: Solver, unit: Unit, mig: int) -> bool:
        history = self.analysis.history
        if isinstance(unit, Life):
            start, end = unit.start_pos, unit.end_pos
        else:
            pair = next((p for p in solver.plan.superseded if p[0] is unit), None)
            if pair is None:
                return False
            start, end = pair[0].pos, pair[1].pos
        if history.ancestors[start[0]] >> mig & 1:
            return False
        return not (end is not None and history.descendants[end[0]] >> mig & 1)

    def _related(self, units: Iterable[Unit], table: str) -> list[Unit]:
        table = table.lower()
        related = []
        for u in units:
            life = u if isinstance(u, Life) else u.owner
            if life is None:
                continue
            model = life.model
            tables = {t.lower() for t in model.tables} | {
                f"{model.app_label}_{n}".lower() for n in model.names
            }
            if any(table == t or table.startswith(t + "_") for t in tables):
                related.append(u)
        return related

    # Result

    def _result(
        self,
        solver: Solver,
        built: Any,
        static_units: set[Unit],
        trial_blocked: Mapping[Unit, str],
        fallback: bool,
        begin: float,
    ) -> Result:
        result = make_result(self.analysis, solver, built)
        final_units = _removed_units(solver)
        unlocked = [u for u in final_units if u not in static_units]
        result.verification = {
            "mode": "trial",
            "vendor": self.vendor,
            "database": self.alias,
            "passes": self.passes,
            "unlocked": len(unlocked),
            "trial_blocked": len(trial_blocked),
            "fallback": fallback,
            "can_rollback_ddl": self.can_rollback_ddl,
            "nondeterministic_tables": sorted(self.masked),
            "failures": [
                {k: v for k, v in asdict(f).items() if k != "traceback"}
                | {"migration": list(f.migration)}
                for f in self.harvested
            ],
            "seconds": round(time.perf_counter() - begin, 3),
        }
        return result


def _named_in(unit: Unit, message: str) -> bool:
    life = unit if isinstance(unit, Life) else unit.owner
    if life is None:
        return False
    text = Text(message, match_all=False)
    return any(text.mentions(name) for name in life.scan_names())


def _removed_units(solver: Solver) -> set[Unit]:
    units: set[Unit] = set(solver.plan.removed)
    units.update(e for e, _ in solver.plan.superseded)
    return units


def _trimmed(exc: BaseException) -> str:
    lines = traceback.format_exception(exc)
    return "".join(lines[-6:])


def run_trial(
    analysis: Analysis,
    *,
    alias: str = "default",
    max_runs: int = 8,
    progress: Progress | None = None,
) -> Result:
    return Trial(analysis, alias, max_runs, progress).run()
