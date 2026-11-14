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
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.operations.base import Operation

from django_clean_migrations.core import (
    FOLD_PREFIX,
    Analysis,
    Built,
    CleanError,
    Result,
    Solution,
    UnitInfo,
    build,
    make_result,
)
from django_clean_migrations.history import Key, OpRecord
from django_clean_migrations.isolate import ChildError, Send, run_isolated
from django_clean_migrations.opaque import OpaqueOp, Text
from django_clean_migrations.ops import OPAQUE, classify
from django_clean_migrations.runner import DatabaseForwards, RunError, run_history
from django_clean_migrations.scratch import ScratchDatabaseError, scratch_database
from django_clean_migrations.snapshot import Difference, Mask, Snapshot, take_snapshot

if TYPE_CHECKING:
    pass

Progress = Callable[[str, bool], None]
"""Called with (message, note): notes are worth keeping, others are status."""

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


class Trial:
    def __init__(
        self,
        analysis: Analysis,
        alias: str = "default",
        max_runs: int = 8,
        progress: Progress | None = None,
        isolate: bool = True,
        single_baseline: bool = False,
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
        self.can_rollback_ddl = False
        self.harvested: list[TrialFailure] = []
        self.masked: set[str] = set()
        """Tables found nondeterministic late (see _mask_nondeterminism)."""
        self.mask = Mask()
        """What differs between two runs of the original history."""
        self.single_baseline = single_baseline
        self.at_risk_units = 0
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
        self, label: str, operations: Mapping[Key, Sequence[Operation]] | None
    ) -> PassResult:
        """
        Migrate a fresh scratch database, in a forked child process (see
        isolate.py), and snapshot it.
        """
        try:
            with scratch_database(self.alias) as connection:
                self.vendor = connection.vendor
                self.can_rollback_ddl = bool(connection.features.can_rollback_ddl)

                def child(send: Send) -> PassResult:
                    return self._migrate(
                        connection, label, operations, lambda m: send((m, False))
                    )

                return run_isolated(
                    child,
                    lambda message: self._say(*message),
                    fork=self.isolate,
                )
        except ScratchDatabaseError as exc:
            raise CleanError(str(exc)) from exc
        except ChildError as exc:
            raise CleanError(
                f"{label} crashed: {exc}\n{exc.child_traceback}{ENVIRONMENT_HINT}"
            ) from exc

    def _migrate(
        self,
        connection: BaseDatabaseWrapper,
        label: str,
        operations: Mapping[Key, Sequence[Operation]] | None,
        say: Callable[[str], None],
    ) -> PassResult:
        failures: list[TrialFailure] = []
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

        hook = self._hook(connection, failures, current, harvest)
        try:
            run_history(
                connection, operations=operations, hook=hook, progress=on_progress
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
            return PassResult(failures, None, True)
        say(f"{label}: taking a snapshot")
        return PassResult(failures, take_snapshot(connection), False)

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
        baseline_begin = time.perf_counter()
        baseline = self._run("Baseline", None)
        baseline_seconds = time.perf_counter() - baseline_begin
        if baseline.aborted or baseline.snapshot is None:
            failure = baseline.failures[0] if baseline.failures else None
            raise CleanError(
                "The original migration history doesn't migrate a fresh"
                f" {self.vendor or 'scratch'} database, so it can't be trialled:"
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
        static = self.analysis.solve()
        # Folds can still be blamed: a snapshot difference on a model's table
        # may come from one (database object names), unlike pass 1/2 removals.
        self.static_units = {
            k for k in static.unit_keys() if not k.startswith(FOLD_PREFIX)
        }
        static_removed = make_result(
            self.analysis, static, build(self.analysis, static)
        ).removed_positions
        first = self.analysis.solve(aggressive=True, base=static)
        candidates = first.unit_keys() - static.unit_keys()
        self.at_risk_units = len(candidates)
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
                raise CleanError(
                    "The original migration history migrated a fresh database"
                    " once, but not a second time"
                    + (f": {failure.error}" if failure else "")
                    + ENVIRONMENT_HINT
                )
            self.mask = reference.nondeterminism(again.snapshot)
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
        if not candidates:
            self._say("Nothing to unlock: only the static plan is trialled.", note=True)

        solution = static
        built = build(self.analysis, solution)
        outcome = self._candidate(solution, built, reference)
        if outcome is not None:
            failures, diffs = outcome
            problems = [f.describe() for f in failures] + [str(d) for d in diffs]
            raise CleanError(
                "Even the static plan failed its trial run, so nothing can be"
                " planned with --trial: "
                + "; ".join(problems[:5])
                + "".join(f"\n{f.traceback}" for f in failures[:1])
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

    def _candidate(
        self, solution: Solution, built: Built, reference: Snapshot
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
        if any(u.at_risk for u in solution.units):
            return diffs
        for d in diffs:
            if d.kind == "data" and d.table and not _related(solution.units, d.table):
                self.masked.add(d.table)
        return reference.differences(snapshot, self.mask, self.masked)

    # Attribution

    def _attribute(
        self,
        solution: Solution,
        failures: list[TrialFailure],
        diffs: list[Difference],
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
        units = [u for u in solution.units if u.key not in self.static_units]
        blocks: dict[str, str] = {}
        history = self.analysis.history
        vague_seen = False
        for failure in failures:
            reason = failure.describe()
            named = _missing(failure.error, units)
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
            keys = sorted(u.key for u in targets)
            suspects = self.suspects.pop(signature, None)
            if suspects is not None:
                keys = sorted(set(keys) & suspects) or keys
            elif len(keys) > 1:
                self.suspects[signature] = set(keys[len(keys) // 2 :])
                keys = keys[: len(keys) // 2]
            for key in keys:
                blocks.setdefault(key, reason)
        for d in diffs:
            reason = f"trial: {d}"
            targets = _related(units, d.table) if d.table else []
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
        return blocks

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
        unlocked = solution.unit_keys() - static.unit_keys()
        result.verification = {
            "mode": "trial",
            "vendor": self.vendor,
            "database": self.alias,
            "runs": self.passes + (1 if self.single_baseline else 2),
            "baseline_runs": 1 if self.single_baseline else 2,
            "at_risk": self.at_risk_units,
            "unlocked": len(unlocked),
            "trial_blocked": len(trial_blocked),
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


def _folds(keys: Iterable[str]) -> set[str]:
    return {k for k in keys if k.startswith(FOLD_PREFIX)}


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
) -> Result:
    return Trial(
        analysis, alias, max_runs, progress, single_baseline=single_baseline
    ).run()
