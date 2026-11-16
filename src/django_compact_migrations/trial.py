"""
Trial mode: replace the opaque-operation name scan by evidence.

1. Run the original history on a scratch database and snapshot it; run it
   again, and mask whatever differs between the two (random or time-based
   values).
2. Solve aggressively, on top of the static plan: name-scan and
   soft-reference hits don't block, but are recorded per removal ("at
   risk").
3. Run the candidate's edited operations (in memory, files untouched) on a
   fresh scratch database. Failures in opaque operations are harvested (rolled
   back to a savepoint, recorded, and skipped) where the backend can roll back
   DDL and the migration is atomic; any other failure ends the pass.
4. Block the removals each failure or snapshot difference points at, solve
   again, and repeat until a pass has no failures and matches the baseline.
   If that doesn't happen within the run limit, fall back to the static plan,
   which must pass the same trial.

Every run happens in a forked child process (see isolate.py), with every
database alias pointed at a scratch database (see scratch.py).
"""

from __future__ import annotations

import re
import time
import traceback
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.operations import RunPython, RunSQL
from django.db.migrations.operations.base import Operation

from django_compact_migrations.core import (
    FOLD_PREFIX,
    NOOP_PREFIX,
    Analysis,
    Built,
    CompactError,
    Result,
    Solution,
    UnitInfo,
    build,
    make_result,
    prune_plan,
)
from django_compact_migrations.history import Key, OpRecord
from django_compact_migrations.isolate import ChildError, Send, run_isolated
from django_compact_migrations.noop import Observation, observe, source_concern
from django_compact_migrations.opaque import OpaqueOp, Text
from django_compact_migrations.ops import OPAQUE, classify
from django_compact_migrations.prune import PrunePlan
from django_compact_migrations.runner import DatabaseForwards, RunError, run_history
from django_compact_migrations.scratch import ScratchDatabaseError, scratch_database
from django_compact_migrations.snapshot import (
    COLUMN_ORDER,
    Difference,
    Mask,
    Snapshot,
    take_snapshot,
)

if TYPE_CHECKING:
    pass

Progress = Callable[[str, bool], None]
"""Called with (message, note): notes are worth keeping, others are status."""

_candidate_hook: Callable[[dict[Key, list[Operation]], int], None] | None = None
"""Test hook: may alter a candidate's operations before pass ``n`` runs."""


class TrialFailed(CompactError):
    """No plan passes its trial run: nothing may be written."""


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

    @property
    def short_error(self) -> str:
        return shorten_error(self.error)

    def describe(self) -> str:
        return (
            f"trial migrate failed in {self.migration[0]}.{self.migration[1]}"
            f" {self.operation}: {self.short_error}"
        )


@dataclass
class PassResult:
    failures: list[TrialFailure]
    snapshot: Snapshot | None
    aborted: bool
    observations: dict[int, Observation] = field(default_factory=dict)
    """rec.seq -> what the operation did (--noop-data, baseline only)."""


class Trial:
    def __init__(
        self,
        analysis: Analysis,
        alias: str = "default",
        max_runs: int = 8,
        progress: Progress | None = None,
        isolate: bool = True,
        single_baseline: bool = False,
        noop_data: bool = False,
        ignore_column_order: bool = False,
        post_migrate: bool = False,
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
        self.origins: dict[int, tuple[int, int]] = {
            rec.seq: (rec.migration.index, rec.index) for rec in history.ops
        }
        self.vendor = ""
        self.server = ""
        """The server's name ("MariaDB" for Django's "mysql" vendor)."""
        self.can_rollback_ddl = False
        self.harvested: list[TrialFailure] = []
        self.masked: set[str] = set()
        """Tables found nondeterministic late (see _mask_nondeterminism)."""
        self.mask = Mask()
        """What differs between two runs of the original history."""
        self.single_baseline = single_baseline
        self.noop_data = noop_data
        self.ignore_column_order = ignore_column_order
        self.post_migrate = post_migrate
        """Send post_migrate at the end of every run, as migrate does."""
        """Don't compare the physical column order of SQLite tables."""
        self.noop: dict[tuple[int, int], str] = {}
        """(plan index, index on disk) of data migrations that did nothing on
        the baseline run, with what they did."""
        self.noop_keys: dict[str, tuple[int, int]] = {}
        self.dropped: list[str] = []
        """Optional layers given up because the plan failed with them."""
        self.wrote: dict[tuple[int, int], frozenset[str]] = {}
        """(plan index, index on disk) of opaque operations -> the tables
        they wrote to on the baseline run."""
        self.noop_seen = 0
        self.noop_suspect: dict[str, str] = {}
        """Data migrations that wrote nothing but whose source suggests
        effects queries don't show, with why."""
        """How many data migrations were observed."""
        self.at_risk_units = 0
        self.first_static: set[str] = set()
        self.candidate_keys: set[str] = set()
        self.passes = 0
        self.static_units: set[str] = set()
        self.isolate = isolate
        """Run each migrate run in a forked child process."""
        self.suspects: dict[tuple[Any, ...], set[str]] = {}
        """Vague failures already half-blamed: the other half of the keys."""

    # Running

    def _say(self, message: str, note: bool = False) -> None:
        """Report progress; ``note`` messages are kept (printed), others
        replace each other (a status line)."""
        if self.progress is not None:
            self.progress(message, note)

    def _run(
        self,
        label: str,
        operations: Mapping[Key, Sequence[Operation]] | None,
        observing: bool = False,
        prune: PrunePlan | None = None,
    ) -> PassResult:
        """
        Migrate a fresh scratch database, in a forked child process (see
        isolate.py), and snapshot it.
        """
        try:
            with scratch_database(self.alias) as connection:
                self.vendor = connection.vendor
                self.server = connection.display_name
                self.can_rollback_ddl = bool(connection.features.can_rollback_ddl)

                def child(send: Send) -> PassResult:
                    return self._migrate(
                        connection,
                        label,
                        operations,
                        lambda m: send((m, False)),
                        observing,
                        prune,
                    )

                return run_isolated(
                    child,
                    lambda message: self._say(*message),
                    fork=self.isolate,
                )
        except ScratchDatabaseError as exc:
            raise CompactError(str(exc)) from exc
        except ChildError as exc:
            raise CompactError(
                f"{label} crashed: {exc}\n{exc.child_traceback}{ENVIRONMENT_HINT}"
            ) from exc

    def _migrate(
        self,
        connection: BaseDatabaseWrapper,
        label: str,
        operations: Mapping[Key, Sequence[Operation]] | None,
        say: Callable[[str], None],
        observing: bool = False,
        prune: PrunePlan | None = None,
    ) -> PassResult:
        failures: list[TrialFailure] = []
        observations: dict[int, Observation] = {}
        current: list[tuple[Key, Operation] | None] = [None]
        harvest = operations is not None
        begin = time.perf_counter()

        def on_progress(event: str, key: Key | None, done: int, total: int) -> None:
            if key is not None:
                elapsed = time.perf_counter() - begin
                say(
                    f"{label}: migration {done + 1}/{total}"
                    f" ({key[0]}.{key[1]}), {elapsed:.0f}s"
                )

        hook = self._hook(
            connection, failures, current, harvest, observations if observing else None
        )
        try:
            run_history(
                connection,
                operations=operations,
                hook=hook,
                progress=on_progress,
                prune=prune,
                post_migrate=self.post_migrate,
            )
        except RunError as exc:
            where = current[0]
            cause = exc.__cause__ or exc
            rec = self.records.get(id(where[1])) if where else None
            failures.append(
                TrialFailure(
                    migration=exc.migration or (where[0] if where else ("?", "?")),
                    operation=type(where[1]).__name__ if where else "?",
                    error=f"{type(cause).__name__}: {cause}",
                    traceback=_trimmed(cause),
                    harvested=False,
                    seq=rec.seq if rec is not None else None,
                )
            )
            return PassResult(failures, None, True, observations)
        say(f"{label}: taking a snapshot")
        return PassResult(failures, take_snapshot(connection), False, observations)

    def _hook(
        self,
        connection: BaseDatabaseWrapper,
        failures: list[TrialFailure],
        current: list[tuple[Key, Operation] | None],
        harvest: bool,
        observations: dict[int, Observation] | None = None,
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

            watched = (
                observations is not None
                and rec is not None
                and opaque is not None
                and not opaque.state_changed
            )

            def database_forwards(*args: Any, **kwargs: Any) -> Any:
                current[0] = (key, op)
                if watched:
                    assert observations is not None and rec is not None
                    observation = observations[rec.seq] = Observation()
                    with observe(connection.alias, observation):
                        return forwards(*args, **kwargs)
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

    def _noop_candidate(self, opaque: OpaqueOp) -> bool:
        """
        Whether a data migration may be removed if it does nothing: an exact
        RunPython or RunSQL without state changes, in an editable migration
        of a selected, not excluded app.
        """
        op = opaque.rec.op
        app = opaque.rec.migration.app_label
        scope = self.analysis.scope
        return (
            (type(op) is RunPython or type(op) is RunSQL)
            and not opaque.state_changed
            and opaque.rec.not_editable_reason() is None
            and app in scope.apps
            and not scope.app_excluded(app)
        )

    def _learn_noop(self, observations: Mapping[int, Observation]) -> None:
        by_seq = {rec.seq: rec for rec in self.analysis.history.ops}
        for seq, observation in observations.items():
            rec = by_seq[seq]
            position = (rec.migration.index, rec.original_index)
            if observation.tables:
                self.wrote[position] = frozenset(observation.tables)
        if not self.noop_data:
            return
        candidates = {
            seq: o
            for seq, o in observations.items()
            if self._noop_candidate(self.opaque[seq])
        }
        self.noop_seen = len(candidates)
        for seq, observation in candidates.items():
            if not observation.no_op:
                continue
            rec = by_seq[seq]
            op = rec.op
            if isinstance(op, RunPython):
                # What doesn't show in queries: signal handlers, backend
                # branches, files, the network...
                concern = source_concern(op.code, observation.statements)
                if concern is not None:
                    self.noop_suspect[f"{rec.migration.label} #{rec.index + 1}"] = (
                        concern
                    )
                    continue
            position = (rec.migration.index, rec.original_index)
            key = f"{NOOP_PREFIX}element:{rec.migration.label}#{rec.original_index}"
            self.noop_keys[key] = position
            self.noop[position] = (
                f"no writes on a fresh {self.server} database: "
                f"{observation.statements}"
                f" {'query' if observation.statements == 1 else 'queries'}"
            )

    # The loop

    def run(self) -> Result:
        begin = time.perf_counter()
        baseline_begin = time.perf_counter()
        # Every opaque operation is watched: what it writes to says which
        # removal to blame for a data difference, and finds no-ops.
        baseline = self._run("Baseline", None, observing=True)
        baseline_seconds = time.perf_counter() - baseline_begin
        if baseline.aborted or baseline.snapshot is None:
            failure = baseline.failures[0] if baseline.failures else None
            raise CompactError(
                "The original migration history doesn't migrate a fresh"
                f" {self.server or 'scratch'} database, so it can't be trialled:"
                + (
                    f" {failure.error} (in {failure.migration[0]}."
                    f"{failure.migration[1]} {failure.operation})"
                    if failure
                    else " unknown error"
                )
                + (f"\n{failure.traceback}" if failure else "")
                + ENVIRONMENT_HINT
            )
        reference = baseline.snapshot
        if self.ignore_column_order:
            self.mask.schema.update(k for k in reference.schema if k[0] == COLUMN_ORDER)
        self._learn_noop(baseline.observations)
        if self.noop_data:
            self._say(
                f"{_count(len(self.noop), 'data migration operation')} of"
                f" {self.noop_seen} did nothing on a fresh {self.server} database"
                + (
                    f"; {len(self.noop_suspect)} more wrote nothing but are kept"
                    " for what their source does ("
                    + ", ".join(
                        f"{k}: {v}" for k, v in sorted(self.noop_suspect.items())[:3]
                    )
                    + (", ..." if len(self.noop_suspect) > 3 else "")
                    + ")"
                    if self.noop_suspect
                    else ""
                )
                + ".",
                note=True,
            )
        # What the static analysis alone proves is blamed last, only when
        # nothing else explains a difference (folds, and what removing no-op
        # data migrations unlocks, are suspects from the start).
        static = self.analysis.solve()
        self.static_units = _proven(static)
        if self.noop:
            static = self.analysis.solve(noop=self.noop)
        static_removed = make_result(
            self.analysis, static, build(self.analysis, static)
        ).removed_positions
        first = self.analysis.solve(aggressive=True, base=static)
        candidates = first.unit_keys() - static.unit_keys()
        self.at_risk_units = len(candidates)
        # Counted against the plan as first solved: the static base can
        # shrink later (blamed folds, no-ops, SQLite column order).
        self.first_static = static.unit_keys()
        self.candidate_keys = set(candidates)
        # The second baseline, the candidates, and the static plan.
        more = (0 if self.single_baseline else 1) + (
            self.max_runs + 1 if candidates else 1
        )
        self._say(
            f"The original history migrated in {baseline_seconds:.0f}s, and"
            " each run takes about as long. A trial can unlock up to"
            f" {_count(len(candidates), 'removal')} that the static scan"
            f" blocks, in at most {_count(more, 'more run')} (about"
            f" {_duration(baseline_seconds * more)} at most).",
            note=True,
        )
        if not self.single_baseline:
            # Two runs of the same history: whatever differs between them is
            # random or time-based, and is never blamed on a removal.
            again = self._run("Baseline, again", None)
            if again.snapshot is None:
                failure = again.failures[0] if again.failures else None
                raise CompactError(
                    "The original migration history migrated a fresh database"
                    " once, but not a second time"
                    + (f": {failure.error}" if failure else "")
                    + ENVIRONMENT_HINT
                )
            ignored = self.mask.schema
            self.mask = reference.nondeterminism(again.snapshot)
            self.mask.schema |= ignored
            if self.mask:
                self._say(
                    f"Differs between two runs of the original history, so not"
                    f" compared: {self.mask.describe()}.",
                    note=True,
                )
        trial_blocked: dict[str, str] = {}
        fallback = False
        solution: Solution | None = None
        while candidates:
            if self.passes >= self.max_runs:
                fallback = True
                break
            solution = self.analysis.solve(
                aggressive=True, trial_blocked=trial_blocked, base=static
            )
            built = build(self.analysis, solution)
            outcome = self._candidate(solution, built, reference)
            if outcome is None:
                result = make_result(self.analysis, solution, built)
                missing = static_removed - result.removed_positions
                if not missing:
                    return self._result(
                        result, solution, static, trial_blocked, fallback, begin
                    )
                # The trial must never do worse than the static plan: blocking
                # one removal can change how others are expressed.
                self._say(
                    "The trial's plan keeps"
                    f" {_count(len(missing), 'operation')} that the static plan"
                    " removes; using the static plan.",
                    note=True,
                )
                fallback = True
                break
            blocks = self._attribute(solution, outcome[0], outcome[1])
            fresh = {k: r for k, r in blocks.items() if k not in trial_blocked}
            if not fresh:
                # Nothing at risk explains it: what the static analysis
                # removes isn't proven after all.
                blocks = self._attribute(
                    solution, outcome[0], outcome[1], include_static=True
                )
                fresh = {k: r for k, r in blocks.items() if k not in trial_blocked}
            self._say(
                f"Run {self.passes}: {_count(len(outcome[0]), 'failure')},"
                f" {_count(len(outcome[1]), 'difference')}; blocked"
                f" {len(fresh)}"
                + (": " + ", ".join(sorted(fresh)[:5]) if fresh else "")
                + (", ..." if len(fresh) > 5 else "")
                + "".join(f"\n  {f.describe()}" for f in outcome[0][:5])
                + "".join(f"\n  {d}" for d in outcome[1][:5]),
                note=True,
            )
            if not fresh:
                fallback = True
                break
            trial_blocked.update(fresh)
            if set(fresh) & static.unit_keys():
                # Part of the plan candidates build on is wrong after all (a
                # fold, a no-op data migration, or what that one unlocked):
                # solve it again without.
                for key in fresh:
                    if key in self.noop_keys:
                        self.noop.pop(self.noop_keys[key], None)
                static = self.analysis.solve(
                    noop=self.noop, trial_blocked=trial_blocked
                )
                static_removed = make_result(
                    self.analysis, static, build(self.analysis, static)
                ).removed_positions
        if not candidates:
            self._say("Nothing to unlock: only the static plan is trialled.", note=True)

        # The static plan (with what the trial blocked) must pass its own
        # run: nothing in it is proven once a run of it differs. Each run
        # blocks something more (of finitely many removals), or drops a
        # layer, or ends.
        while True:
            solution = static
            built = build(self.analysis, solution)
            outcome = self._candidate(solution, built, reference)
            if outcome is None:
                break
            failures, diffs = outcome
            blocks = self._attribute(solution, failures, diffs, include_static=True)
            fresh = {
                k: r
                for k, r in blocks.items()
                if k not in trial_blocked and k in static.unit_keys()
            }
            problems = [f.describe() for f in failures] + [str(d) for d in diffs]
            if fresh:
                self._say(
                    f"Run {self.passes} (static plan): blocked {len(fresh)}: "
                    + ", ".join(sorted(fresh)[:5])
                    + (", ..." if len(fresh) > 5 else "")
                    + "".join(f"\n  {p}" for p in problems[:5]),
                    note=True,
                )
                trial_blocked.update(fresh)
                for key in fresh:
                    if key in self.noop_keys:
                        self.noop.pop(self.noop_keys[key], None)
                static = self.analysis.solve(
                    noop=self.noop, trial_blocked=trial_blocked
                )
                continue
            # Nothing left to blame: drop an optional layer, or give up.
            if self._drop_layer(problems):
                static = self.analysis.solve(
                    noop=self.noop, trial_blocked=trial_blocked
                )
                continue
            raise TrialFailed(
                "The trial found no plan that migrates a fresh"
                f" {self.server} database like the original history, so"
                " nothing was planned or written: "
                + "; ".join(problems[:5])
                + ENVIRONMENT_HINT
            )
        return self._result(
            make_result(self.analysis, solution, built),
            solution,
            static,
            trial_blocked,
            fallback,
            begin,
        )

    def _drop_layer(self, problems: list[str]) -> bool:
        """
        The static plan failed its trial and nothing in it can be blamed:
        drop the next optional layer (--prune, then --noop-data), say so,
        and return whether there was one.
        """
        reason = problems[0]
        if self.analysis.options.get("prune"):
            self.analysis.options["prune"] = False
            label = "--prune"
        elif self.noop:
            self.noop.clear()
            self.noop_keys.clear()
            label = "--noop-data"
        else:
            return False
        self.dropped.append(f"{label} ({reason})")
        self._say(
            f"The plan failed its trial run with {label}; trying without it: {reason}",
            note=True,
        )
        return True

    def _candidate(
        self, solution: Solution, built: Built, reference: Snapshot
    ) -> tuple[list[TrialFailure], list[Difference]] | None:
        """Run a candidate; None if it passes, else failures and differences."""
        operations = built.operations_by_key(self.analysis.history)
        if _candidate_hook is not None:
            _candidate_hook(operations, self.passes)
        prune = (
            prune_plan(self.analysis, built)
            if self.analysis.options.get("prune")
            else None
        )
        if prune:
            # The pruned history doesn't record the pruned migrations.
            reference = reference.without_migrations(prune.pruned)
        self.passes += 1
        result = self._run(f"Trial run {self.passes}", operations, prune=prune)
        self.harvested.extend(result.failures)
        if result.failures or result.snapshot is None:
            # Skipped failing operations explain any snapshot differences,
            # so only the failures are attributed this time.
            return result.failures, []
        diffs = reference.differences(result.snapshot, self.mask, self.masked)
        if diffs:
            diffs = self._mask_nondeterminism(
                solution, diffs, reference, result.snapshot
            )
        if not diffs:
            return None
        return [], diffs

    def _mask_nondeterminism(
        self,
        solution: Solution,
        diffs: list[Difference],
        reference: Snapshot,
        snapshot: Snapshot,
    ) -> list[Difference]:
        """
        Data differences in tables no removal relates to, when nothing at risk
        is left to blame, come from the history itself (random or time-based
        values in seeded rows): mask those tables rather than loop.
        """
        if not self.single_baseline or any(u.at_risk for u in solution.units):
            # With two baseline runs, what differs between runs of the
            # original history is known: any other difference is real.
            return diffs
        for d in diffs:
            if (
                d.kind == "data"
                and d.table
                and d.table != "django_migrations"
                and not _related(solution.units, d.table)
            ):
                self.masked.add(d.table)
        return reference.differences(snapshot, self.mask, self.masked)

    # Attribution

    def _attribute(
        self,
        solution: Solution,
        failures: list[TrialFailure],
        diffs: list[Difference],
        *,
        include_static: bool = False,
    ) -> dict[str, str]:
        """
        Which removals to block for this run's failures and differences.

        A failure that names what is missing ("Cannot resolve keyword
        'legacy'", "doesn't have a 'Coupon' model", 'column "x" does not
        exist') blocks exactly the removals of that field, model, table or
        app. Otherwise it blocks the removals the failing operation mentions
        (narrowed by the names in the message), or else those whose
        lifetime includes the failing migration, half at a time: the other
        half is only blocked if the same failure comes back. Only the first
        such vague failure of a run is used: later ones may just follow from
        it (the skipped operation did nothing), and are re-tested next run.
        """
        # The static plan's pass 1/2 removals are proven without a trial; if
        # nothing else is to blame, the loop falls back to the static plan,
        # which is trialled too.
        units = [
            u
            for u in solution.units
            if include_static or u.key not in self.static_units
        ]
        blocks: dict[str, str] = {}
        history = self.analysis.history
        vague_seen = False
        for failure in failures:
            reason = failure.describe()
            # Evidence counts against any removal, the static plan's too.
            named = _missing(failure.error, units) or _missing(
                failure.error, solution.units
            )
            origin = self.origins.get(failure.seq) if failure.seq is not None else None
            mentioned_by_op = [u for u in named if origin in u.at_risk]
            # "Cannot resolve keyword 'user'" doesn't say of which model: the
            # removals the failing operation mentions, if any, are meant.
            named = mentioned_by_op or named
            if named:
                for u in named:
                    blocks.setdefault(u.key, reason)
                continue
            targets: list[UnitInfo] = []
            origin = self.origins.get(failure.seq) if failure.seq is not None else None
            if origin is not None and failure.seq in self.opaque:
                targets = [u for u in units if origin in u.at_risk]
                mentioned = [u for u in targets if _named_in(u, failure.error)]
                if mentioned:
                    targets = mentioned
            if not targets and failure.migration in history.by_key:
                mig = history.by_key[failure.migration].index
                targets = [u for u in units if self._in_window(u, mig)]
                if failure.seq not in self.opaque:
                    # A schema operation failing: most likely something
                    # folded into an operation of that migration, now created
                    # before what it needs (an extension, a function...).
                    folded_here = [
                        u
                        for u in targets
                        if u.key.startswith(FOLD_PREFIX) and u.start == mig
                    ]
                    targets = folded_here or targets
                # Narrowed to those the message names (a table, a column, a
                # model), if any.
                lowered = failure.error.lower()
                named_here = [
                    u
                    for u in targets
                    if _named_in(u, failure.error)
                    or any(t in lowered for t in u.tables)
                ]
                targets = named_here or targets
            if vague_seen:
                continue
            vague_seen = True
            signature = (failure.migration, failure.seq, failure.error.split(":")[0])
            for key in self._halve(signature, [u.key for u in targets]):
                blocks.setdefault(key, reason)
        for d in diffs:
            reason = f"trial: {d}"
            if d.detail.startswith(COLUMN_ORDER):
                # Even what the static analysis proves can move columns on
                # SQLite (it reasons about the project state, not about
                # which table rebuilds happen).
                # Whole dead fields and models don't move the columns that
                # stay; skipping or moving an AlterField/RenameField does.
                targets = [
                    u
                    for u in (_related(solution.units, d.table) if d.table else [])
                    if u.key.startswith((FOLD_PREFIX, "element:"))
                ]
                for u in targets:
                    blocks.setdefault(u.key, reason)
                continue
            if d.kind == "data":
                # Rows written by an opaque operation that a removal took
                # away (an elidable one, a no-op that wasn't): whatever
                # proves the removal otherwise.
                table = (d.table or "").lower()
                writers = [
                    u.key
                    for u in solution.units
                    if any(
                        table in tables or "*" in tables
                        for tables in (
                            self.wrote.get(p, frozenset()) for p in u.removed_opaque
                        )
                    )
                ]
                if writers:
                    for key in writers:
                        blocks.setdefault(key, reason)
                    continue
            targets = (
                _related(units, d.table) or _related(solution.units, d.table)
                if d.table
                else []
            )
            if d.columns:
                # Only those changing the columns that differ, if any do.
                named = [
                    u for u in targets if u.fields & {c.lower() for c in d.columns}
                ]
                targets = named or targets
            if not targets and d.kind == "data":
                targets = [u for u in units if u.at_risk]
            for u in targets:
                blocks.setdefault(u.key, reason)
        # No evidence names any: half of them, the rest if it persists. Not
        # for what happens outside the migrations (the pruned graph's checks)
        # or in django_migrations: an optional layer explains those.
        located = [f.describe() for f in failures if f.migration in history.by_key]
        located += [str(d) for d in diffs if d.table != "django_migrations"]
        if include_static and not blocks and units and located:
            for key in self._halve(
                ("unattributed", *sorted(located)), [u.key for u in units]
            ):
                blocks[key] = f"trial: {located[0]}"
        return blocks

    def _halve(self, signature: tuple[Any, ...], keys: Iterable[str]) -> list[str]:
        """
        Suspects to block for a problem that doesn't say which is to blame:
        half of them the first time; if the same problem comes back, the
        rest.
        """
        ordered = sorted(keys)
        suspects = self.suspects.pop(signature, None)
        if suspects is not None:
            return sorted(set(ordered) & suspects) or ordered
        if len(ordered) > 1:
            self.suspects[signature] = set(ordered[len(ordered) // 2 :])
            return ordered[: len(ordered) // 2]
        return ordered

    def _in_window(self, unit: UnitInfo, mig: int) -> bool:
        history = self.analysis.history
        if history.ancestors[unit.start] >> mig & 1:
            return False
        return not (unit.end is not None and history.descendants[unit.end] >> mig & 1)

    # Result

    def _result(
        self,
        result: Result,
        solution: Solution,
        static: Solution,
        trial_blocked: Mapping[str, str],
        fallback: bool,
        begin: float,
    ) -> Result:
        unlocked = solution.unit_keys() - self.first_static
        self.candidate_keys |= unlocked
        result.verification = {
            "mode": "trial",
            "vendor": self.vendor,
            "server": self.server,
            "database": self.alias,
            "runs": self.passes + (1 if self.single_baseline else 2),
            "baseline_runs": 1 if self.single_baseline else 2,
            "at_risk": len(self.candidate_keys),
            "unlocked": len(unlocked),
            "noop": [
                {
                    "migration": list(self.analysis.history.migrations[pos[0]].key),
                    "index": pos[1],
                    "note": note,
                }
                for pos, note in sorted(self.noop.items())
            ]
            if self.noop_data
            else None,
            "noop_kept": dict(sorted(self.noop_suspect.items()))
            if self.noop_data
            else None,
            "trial_blocked": len(trial_blocked),
            "dropped": list(self.dropped),
            "fallback": fallback,
            "can_rollback_ddl": self.can_rollback_ddl,
            "nondeterministic_tables": sorted(self.masked | self.mask.tables),
            "nondeterministic_columns": {
                t: sorted(c) for t, c in sorted(self.mask.columns.items())
            },
            "failures": [
                {k: v for k, v in asdict(f).items() if k != "traceback"}
                | {"migration": list(f.migration), "error": f.short_error}
                for f in self.harvested
            ],
            "seconds": round(time.perf_counter() - begin, 3),
        }
        return result


_MISSING = (
    (re.compile(r"Cannot resolve keyword '([^']+)'"), "field"),
    (re.compile(r"(?:'(?P<model>\w+)' object )?has no attribute '([^']+)'"), "field"),
    (re.compile(r"(?:(?P<model>\w+) )?has no field named '([^']+)'"), "field"),
    (re.compile(r"unexpected keyword arguments?: '([^']+)'"), "field"),
    (re.compile(r"column \"?([\w.]+)\"? does not exist", re.I), "field"),
    (re.compile(r"no such column: ([\w.]+)"), "field"),
    (re.compile(r"Unknown column '([^']+)'"), "field"),
    (re.compile(r"doesn't have a '([^']+)' model"), "model"),
    (re.compile(r"No installed app with label '([^']+)'"), "app"),
    (re.compile(r"relation \"([^\"]+)\" does not exist"), "table"),
    (re.compile(r"no such table: ([\w.]+)"), "table"),
)


def _missing(message: str, units: Iterable[UnitInfo]) -> list[UnitInfo]:
    """
    Units removing what an error message says is missing. Folds never make
    anything missing (they create things earlier, never later), so they are
    left out; when the message names the model, only units of that model
    count.
    """
    candidates = [u for u in units if not u.key.startswith(FOLD_PREFIX)]
    found: list[UnitInfo] = []
    for pattern, kind in _MISSING:
        for match in pattern.finditer(message):
            # The missing name is the last group (the model, if any, first).
            name = (
                match.group(match.re.groups).lower().rsplit(".", 1)[-1].split("__")[0]
            )
            model = (match.groupdict().get("model") or "").lower()
            matched = [
                u
                for u in candidates
                if u not in found
                and (
                    (kind == "field" and name in u.fields)
                    or (kind == "model" and name in u.models)
                    or (kind == "app" and u.app == name and u.models)
                    or (kind == "table" and name in u.tables)
                )
            ]
            if model:
                of_model = [
                    u for u in matched if any(t.endswith("_" + model) for t in u.tables)
                ]
                matched = of_model or matched
            found.extend(matched)
    return found


def _related(units: Iterable[UnitInfo], table: str) -> list[UnitInfo]:
    table = table.lower()
    return [
        u
        for u in units
        if any(table == t or table.startswith(t + "_") for t in u.tables)
    ]


def _named_in(unit: UnitInfo, message: str) -> bool:
    text = Text(message, match_all=False)
    return any(text.mentions(name) for name in unit.names)


ENVIRONMENT_HINT = (
    "\nIf the history migrates fine with 'migrate', the cause may be the"
    " environment of the scratch runs rather than the history: data migrations"
    " or signal handlers using another database alias (every alias points at"
    " an empty scratch database), pre_migrate handlers, settings, or state"
    " kept in the process."
)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _proven(solution: Solution) -> set[str]:
    """
    The static plan's removals blamed only when nothing else explains a
    difference: not folds (a snapshot difference may come from one: database
    object names), nor no-op data migrations (only shown harmless on the
    baseline).
    """
    return {
        k for k in solution.unit_keys() if not k.startswith((FOLD_PREFIX, NOOP_PREFIX))
    }


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f} min"


def shorten_error(message: str, limit: int = 200) -> str:
    """Error messages for reports: without FieldError's list of choices,
    and not too long."""
    message = re.sub(r"\. Choices are: .*", ". (choices omitted)", message, flags=re.S)
    message = " ".join(message.split())
    return message if len(message) <= limit else message[: limit - 3] + "..."


def _trimmed(exc: BaseException) -> str:
    lines = traceback.format_exception(exc)
    return "".join(lines[-6:])


def run_trial(
    analysis: Analysis,
    *,
    alias: str = "default",
    max_runs: int = 8,
    progress: Progress | None = None,
    single_baseline: bool = False,
    noop_data: bool = False,
    ignore_column_order: bool = False,
    post_migrate: bool = False,
) -> Result:
    return Trial(
        analysis,
        alias,
        max_runs,
        progress,
        single_baseline=single_baseline,
        noop_data=noop_data,
        ignore_column_order=ignore_column_order,
        post_migrate=post_migrate,
    ).run()
