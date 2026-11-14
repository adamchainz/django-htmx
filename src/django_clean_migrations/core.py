"""
Library entry point: ``plan()`` analyses, ``Result.save()`` / ``load_plan()``
store and restore a plan, ``Result.write()`` applies it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import tempfile
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.apps import apps
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ProjectState

from django_clean_migrations.history import (
    History,
    Key,
    OpRecord,
    derive_history,
    invalidate_modules,
    load_history,
)
from django_clean_migrations.locate import locate_create_model, locate_together_op
from django_clean_migrations.opaque import TextBuilder
from django_clean_migrations.rewrite import (
    OpEdit,
    RewriteError,
    derive_op_edit,
    edited_operation,
    regeneration_problem,
    rewrite_source,
    unchanged,
)
from django_clean_migrations.solver import (
    OPAQUE_POLICIES,
    Scope,
    SelectionError,
    Solver,
    Target,
    Unit,
    kinds_from,
)
from django_clean_migrations.timeline import (
    Element,
    FieldLife,
    Life,
    ModelLife,
    Timeline,
    TogetherLife,
    describe_sub,
)
from django_clean_migrations.verify import (
    VerificationError,
    state_from_disk,
    verify_in_memory,
    verify_on_disk,
)

__all__ = [
    "Blocked",
    "CleanError",
    "Location",
    "Removal",
    "Result",
    "load_plan",
    "plan",
]


class CleanError(Exception):
    """Invalid options or an unusable migration history."""


@dataclass(frozen=True)
class Location:
    migration: Key
    path: Path | None
    line: int | None
    operation: str
    detail: str | None = None

    def display_path(self) -> str:
        if self.path is None:
            return f"{self.migration[0]}.{self.migration[1]}"
        try:
            return os.path.relpath(self.path)
        except ValueError:
            return str(self.path)


@dataclass(frozen=True)
class Removal:
    kind: str
    """model, field, index, constraint, together, superseded or elidable."""
    app_label: str
    model: str | None
    label: str
    description: str
    elements: tuple[Location, ...]
    name: str = ""
    """What was removed within its model, e.g. a field or index name."""
    start: str = ""
    """Migration where it first appeared."""
    end: str = ""
    """Migration where it was removed or replaced, if any."""
    note: str = ""
    """Extra context, e.g. why an elidable operation is removed."""
    targets: tuple[Location, ...] = ()
    """Operations regenerated to absorb the removed ones (folds)."""


@dataclass(frozen=True)
class Blocked:
    kind: str
    app_label: str
    model: str | None
    label: str
    reasons: tuple[str, ...]
    description: str = ""
    """Where it lived, e.g. "field added in 0002_x, removed in 0005_y"."""


@dataclass(frozen=True)
class FileChange:
    path: Path
    migration: Key
    module: str
    original: str
    new: str
    operations_after: tuple[str, ...] = ()
    """Operation class names the rewritten migration must load with."""
    expected_sha256: str | None = None
    """For a loaded plan: the hash of the content the plan was made from."""

    @property
    def sha256_before(self) -> str:
        if self.expected_sha256 is not None:
            return self.expected_sha256
        return sha256_text(self.original)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Result:
    removals: list[Removal]
    blocked: list[Blocked]
    warnings: list[str]
    files: list[FileChange]
    removed_operations: int
    edited_operations: int
    empty_migrations: list[Key]
    folded_operations: int = 0
    """Operations deleted by folding them into earlier ones (not counted in
    removed_operations)."""
    options: dict[str, Any] = field(default_factory=dict)
    """The options the plan was made with, as passed to ``plan()``."""
    verification: dict[str, Any] = field(default_factory=lambda: {"mode": "static"})
    """How the plan was checked before being reported."""
    removed_positions: frozenset[tuple[Key, int]] = frozenset()
    """(migration, operation index) of every operation removed outright."""
    edited_positions: frozenset[tuple[Key, int]] = frozenset()
    """(migration, operation index) of every operation edited."""
    _plan_keys: list[Key] | None = field(default=None, repr=False)
    _final_state: ProjectState | None = field(default=None, repr=False)

    @property
    def changed(self) -> bool:
        return bool(self.files)

    def save(self, path: str | os.PathLike[str]) -> None:
        """Write the plan as a reviewable JSON file for ``load_plan()``."""
        from django_clean_migrations.planfile import save_plan

        save_plan(self, Path(path))

    def write(self) -> None:
        """
        Write the rewritten files, then reload them from disk and check the
        resulting project state; on any failure restore every file and raise
        CleanError.

        For a loaded plan, every file must still have the content the plan
        was made from.
        """
        self._check_unchanged()
        if self._final_state is None or self._plan_keys is None:
            _forget(self.files)
            try:
                _, plan_keys, final_state = state_from_disk()
            except Exception as exc:
                raise CleanError(f"Could not load migrations: {exc}") from exc
        else:
            plan_keys, final_state = self._plan_keys, self._final_state
        expected = {c.migration: list(c.operations_after) for c in self.files}
        # Registered before writing: a failure (or interrupt) mid-write must
        # still restore that file.
        touched: list[FileChange] = []
        try:
            for change in self.files:
                touched.append(change)
                _write_text(change.path, change.new)
            _forget(self.files)
            verify_on_disk(plan_keys, final_state, expected)
        except BaseException as exc:
            unrestored: list[str] = []
            for change in touched:
                try:
                    _write_text(change.path, change.original)
                except OSError as restore_exc:
                    unrestored.append(f"{change.path} ({restore_exc})")
            _forget(self.files)
            if unrestored:
                raise CleanError(
                    f"Writing the migration files failed ({exc!r}) and these"
                    f" could not be restored: {', '.join(unrestored)}."
                ) from exc
            if not isinstance(exc, Exception):
                raise
            raise CleanError(
                "Writing or verifying the rewritten migration files failed, so"
                f" they were restored: {exc}. Please report this as a bug."
            ) from exc

    def _check_unchanged(self) -> None:
        stale = []
        for change in self.files:
            try:
                current = _read_text(change.path)
            except OSError:
                stale.append(f"{change.path} (missing)")
                continue
            if sha256_text(current) != change.sha256_before:
                stale.append(str(change.path))
        if stale:
            raise CleanError(
                "These migration files changed since the plan was made, so"
                f" nothing was written: {', '.join(stale)}. Make a new plan."
            )


def _read_text(path: Path) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _write_text(path: Path, text: str) -> None:
    """Replace ``path`` atomically, keeping its permissions."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        try:
            shutil.copymode(path, tmp)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _forget(changes: Sequence[FileChange]) -> None:
    invalidate_modules([c.module for c in changes], [c.path for c in changes])


def _location(e: Element) -> Location:
    rec = e.rec
    line = rec.line
    detail = None
    if e.sub is not None:
        detail = describe_sub(e.sub)
        line = _sub_line(e) or line
    return Location(rec.migration.key, rec.migration.path, line, rec.name, detail)


def _sub_line(e: Element) -> int | None:
    sub = e.sub
    assert sub is not None
    rec = e.rec
    if sub[0] == "entry":
        tloc = locate_together_op(rec)
        if isinstance(tloc, str) or tloc.node is None:
            return None
        node = tloc.node
        if tloc.flat or not hasattr(node, "elts"):
            return node.lineno
        return node.elts[tloc.entries[sub[1]]].lineno  # type: ignore[no-any-return]
    loc = locate_create_model(rec)
    if isinstance(loc, str):
        return None
    if sub[0] == "field":
        return loc.fields.elts[loc.field_index[sub[1]]].lineno
    if sub[0] in ("index", "constraint"):
        key = "indexes" if sub[0] == "index" else "constraints"
        index: int = sub[1]
        return loc.lists[key].elts[index].lineno
    tloc2 = loc.together.get(sub[1])
    if tloc2 is None or tloc2.node is None:
        return None
    if tloc2.flat or not hasattr(tloc2.node, "elts"):
        return tloc2.node.lineno
    return tloc2.node.elts[tloc2.entries[sub[2]]].lineno  # type: ignore[no-any-return]


def _mig_name(rec: OpRecord, app_label: str) -> str:
    mig = rec.migration
    return mig.name if mig.app_label == app_label else mig.label


def _describe_life(life: Life) -> str:
    assert life.creation is not None and life.end is not None
    app = life.app_label
    created = _mig_name(life.creation.rec, app)
    ended = _mig_name(life.end, app)
    if isinstance(life, ModelLife):
        return f"model created in {created}, deleted in {ended}"
    if isinstance(life, FieldLife):
        return f"field added in {created}, removed in {ended}"
    if isinstance(life, TogetherLife):
        names = ", ".join(f.names[-1] for f in life.fields)
        return f"{life.option} ({names}) added in {created}, removed in {ended}"
    return f"{life.kind} {life.names[0]!r} added in {created}, removed in {ended}"


def _entity_name(life: Life) -> str:
    if isinstance(life, TogetherLife):
        names = ", ".join(f.names[-1] for f in life.fields)
        return f"{life.option} ({names})"
    if isinstance(life, (ModelLife, FieldLife)):
        return life.names[-1]
    return str(life.names[0])


def _life_label(life: Life) -> str:
    if isinstance(life, (ModelLife, FieldLife)):
        return life.label
    return life.model.label


def _sort_key(e: Element) -> tuple[int, int]:
    return (e.rec.seq, 0)


def plan(
    targets: Sequence[str] = (),
    *,
    exclude: Sequence[str] = (),
    kinds: Collection[str] | None = None,
    skip_kinds: Collection[str] = (),
    opaque: str = "scan",
    elidable: bool = False,
    trial: bool = False,
    database: str = "default",
    trial_max_runs: int = 8,
    trial_single_baseline: bool = False,
    progress: Any = None,
) -> Result:
    """
    Analyse the whole migration history and plan removals. Nothing is written;
    call ``Result.write()`` to apply. Raises CleanError on invalid options or
    if the planned edit fails verification.

    With ``trial``, removals blocked only by the opaque-operation name scan
    are tried for real on scratch databases of ``database``'s backend (see
    ``django_clean_migrations.trial``).
    """
    analysis = analyse(
        targets,
        exclude=exclude,
        kinds=kinds,
        skip_kinds=skip_kinds,
        opaque=opaque,
        elidable=elidable,
    )
    if trial:
        from django_clean_migrations.trial import run_trial

        result: Result = run_trial(
            analysis,
            alias=database,
            max_runs=trial_max_runs,
            progress=progress,
            single_baseline=trial_single_baseline,
        )
        return result
    solution = analysis.solve()
    return make_result(analysis, solution, build(analysis, solution))


MAX_ROUNDS = 10
FOLD_PREFIX = "fold:"
"""Prefix of fold units' keys: folds are tried in trials like scan-blocked
removals (their names in the database depend on more than the state)."""


@dataclass(frozen=True)
class UnitInfo:
    """A removal unit, as a trial run needs to know it."""

    key: str
    start: int
    """Plan index of the migration where its window starts."""
    end: int | None
    tables: frozenset[str]
    """Tables of the model concerned (lower case)."""
    names: frozenset[str]
    at_risk: frozenset[tuple[int, int]]
    """(plan index, index on disk) of opaque operations it relies on."""
    app: str = ""
    fields: frozenset[str] = frozenset()
    """Names and columns of the fields it removes or changes (lower case)."""
    models: frozenset[str] = frozenset()
    """Names of the models it removes (lower case)."""


@dataclass
class Solution:
    """The outcome of solving, over as many fold rounds as needed."""

    operations: dict[int, list[Operation]]
    """Plan index -> final operation list, for changed migrations."""
    origins: dict[int, tuple[int, bool]]
    """id(operation) -> (its index on disk, whether it was edited)."""
    removals: list[Removal]
    blocked: list[Blocked]
    units: list[UnitInfo]
    folded: set[tuple[int, int]]
    """(plan index, index on disk) of operations deleted by folding."""
    rounds: int

    def unit_keys(self) -> set[str]:
        return {u.key for u in self.units}


@dataclass
class Analysis:
    """A loaded and replayed history, ready to be solved repeatedly."""

    history: History
    timeline: Timeline
    scope: Scope
    options: dict[str, Any]
    texts: TextBuilder = field(default_factory=TextBuilder)

    def solve(
        self,
        *,
        aggressive: bool = False,
        trial_blocked: Mapping[str, str] | None = None,
        base: Solution | None = None,
    ) -> Solution:
        """
        Solve, then fold, then solve the folded history again (folds can make
        more things dead, and enable each other) until nothing changes.

        ``base``: a solution to start from (its edits are kept, and solving
        goes on from the history they make), so the result contains all of
        it. Trials start from the static plan, so they never do worse.
        """
        history, timeline = self.history, self.timeline
        operations: dict[int, list[Operation]] = {}
        origins: dict[int, tuple[int, bool]] = {}
        removals: list[Removal] = []
        units: list[UnitInfo] = []
        folded: set[tuple[int, int]] = set()
        rounds = 0
        if base is not None and base.operations:
            operations = dict(base.operations)
            origins = dict(base.origins)
            removals = list(base.removals)
            units = list(base.units)
            folded = set(base.folded)
            history = derive_history(self.history, operations, origins)
            timeline = Timeline(history)
            timeline.replay()
        while True:
            rounds += 1
            solver = Solver(
                history,
                timeline,
                self.scope,
                self.options["opaque"],
                self.options["elidable"],
                aggressive=aggressive,
                trial_blocked=trial_blocked,
                texts=self.texts,
            )
            solver.solve_dead()
            if self.options["elidable"]:
                solver.drop_all_elidable()
            solver.drop_redundant_together()
            solver.solve_superseded()
            if solver.fold_kinds:
                solver.solve_folds(
                    lambda p, op: regeneration_problem(
                        p.rec.migration.on_disk.source, op
                    )
                )
            edits = _collect_edits(solver)
            if not edits or rounds > MAX_ROUNDS:
                break
            removals.extend(_removals(solver))
            units.extend(_units(solver))
            folded.update(
                (f.folded.rec.migration.index, f.folded.rec.original_index)
                for f in solver.plan.folds
            )
            by_migration: dict[int, list[OpRecord]] = {}
            for rec in history.ops:
                by_migration.setdefault(rec.migration.index, []).append(rec)
            for index, op_edits in edits.items():
                new_ops: list[Operation] = []
                for rec in by_migration[index]:
                    edit = op_edits.get(rec.index)
                    if edit is None:
                        new_ops.append(rec.op)
                        origins[id(rec.op)] = (rec.original_index, rec.derived)
                        continue
                    new_op = edited_operation(edit)
                    if new_op is not None:
                        new_ops.append(new_op)
                        origins[id(new_op)] = (rec.original_index, True)
                operations[index] = new_ops
            history = derive_history(self.history, operations, origins)
            timeline = Timeline(history)
            try:
                timeline.replay()
            except Exception as exc:
                raise CleanError(
                    f"Internal error: the folded history doesn't replay ({exc})."
                    " No files were changed. Please report this as a bug, and use"
                    " --skip-kind fold to work around it."
                ) from exc
        removals.sort(key=lambda r: (r.app_label, (r.model or "").lower()))
        return Solution(
            operations,
            origins,
            removals,
            _blocked(solver),
            units,
            folded,
            rounds,
        )


def analyse(
    targets: Sequence[str] = (),
    *,
    exclude: Sequence[str] = (),
    kinds: Collection[str] | None = None,
    skip_kinds: Collection[str] = (),
    opaque: str = "scan",
    elidable: bool = False,
) -> Analysis:
    if opaque not in OPAQUE_POLICIES:
        raise CleanError(f"Unknown opaque policy {opaque!r}.")
    try:
        kind_set = kinds_from(kinds, skip_kinds)
    except SelectionError as exc:
        raise CleanError(str(exc)) from exc

    try:
        history = load_history()
    except Exception as exc:
        raise CleanError(f"Could not load migrations: {exc}") from exc

    app_labels = [a.label for a in apps.get_app_configs()]
    try:
        target_list = [Target.parse(t, app_labels) for t in targets]
        exclude_list = [Target.parse(t, app_labels) for t in exclude]
    except SelectionError as exc:
        raise CleanError(str(exc)) from exc
    default_apps = frozenset(m.app_label for m in history.migrations if m.editable)
    scope = Scope(target_list, exclude_list, kind_set, default_apps)

    timeline = Timeline(history)
    try:
        timeline.replay()
    except Exception as exc:
        raise CleanError(str(exc)) from exc
    for text, target in [*zip(targets, target_list), *zip(exclude, exclude_list)]:
        if target.model is not None and not any(
            _matches_entity(target, life) for life in timeline.lives
        ):
            what = "field" if target.field is not None else "model"
            raise CleanError(
                f"No {what} matching {text!r} exists anywhere in the migration history."
            )
    return Analysis(
        history,
        timeline,
        scope,
        {
            "targets": list(targets),
            "exclude": list(exclude),
            "kinds": sorted(kind_set),
            "opaque": opaque,
            "elidable": elidable,
        },
    )


@dataclass
class Built:
    """A solution's operation lists, verified in memory against the original."""

    edited_ops: dict[int, list[Operation]]
    """Migration plan index -> its operations after editing."""
    expected: dict[Key, list[str]]

    def operations_by_key(self, history: History) -> dict[Key, list[Operation]]:
        return {history.migrations[i].key: ops for i, ops in self.edited_ops.items()}


def build(analysis: Analysis, solution: Solution) -> Built:
    history, timeline = analysis.history, analysis.timeline
    edited_ops = solution.operations
    expected = {
        history.migrations[i].key: [type(op).__name__ for op in ops]
        for i, ops in edited_ops.items()
    }
    try:
        verify_in_memory(history, edited_ops, timeline.dangling, timeline.state)
    except VerificationError as exc:
        raise CleanError(
            f"Internal error: {exc}. No files were changed. Please report this as a"
            " bug, and use --exclude to work around it."
        ) from exc
    return Built(edited_ops, expected)


def make_result(analysis: Analysis, solution: Solution, built: Built) -> Result:
    history, timeline = analysis.history, analysis.timeline
    files: list[FileChange] = []
    removed: set[tuple[Key, int]] = set()
    edited: set[tuple[Key, int]] = set()
    empty: list[Key] = []
    folded_count = 0
    by_migration: dict[int, list[OpRecord]] = {}
    for rec in history.ops:
        by_migration.setdefault(rec.migration.index, []).append(rec)
    for index, final in sorted(built.edited_ops.items()):
        info = history.migrations[index]
        by_origin = {solution.origins[id(op)][0]: op for op in final}
        op_edits: list[OpEdit] = []
        for rec in by_migration.get(index, []):
            new = by_origin.get(rec.index)
            if new is rec.op:
                continue
            if new is None:
                op_edits.append(OpEdit(rec, remove=True))
                removed.add((info.key, rec.index))
                if (index, rec.index) in solution.folded:
                    folded_count += 1
                continue
            if unchanged(rec.op, new):
                # E.g. an AlterField that set the definition the AddField
                # already had: the AddField stays as written.
                continue
            op_edits.append(derive_op_edit(rec, new) or OpEdit(rec, replacement=new))
            edited.add((info.key, rec.index))
        if not op_edits:
            continue
        assert info.source is not None and info.path is not None
        try:
            new_text = rewrite_source(info.source, op_edits)
            compile(new_text, str(info.path), "exec")
        except (SyntaxError, RewriteError) as exc:
            raise CleanError(
                f"Internal error: rewriting {info.label} failed ({exc}). No files"
                " were changed. Please report this as a bug."
            ) from exc
        files.append(
            FileChange(
                info.path,
                info.key,
                type(info.migration).__module__,
                info.source.text,
                new_text,
                tuple(built.expected[info.key]),
            )
        )
        if not final:
            empty.append(info.key)

    return Result(
        removals=solution.removals,
        blocked=solution.blocked,
        warnings=list(history.warnings),
        files=files,
        removed_operations=len(removed) - folded_count,
        edited_operations=len(edited),
        empty_migrations=empty,
        folded_operations=folded_count,
        options=dict(analysis.options),
        removed_positions=frozenset(removed),
        edited_positions=frozenset(edited),
        _plan_keys=list(history.plan),
        _final_state=timeline.state,
    )


def _matches_entity(target: Target, life: Life) -> bool:
    if target.field is None:
        return isinstance(life, ModelLife) and target.matches(life)
    return isinstance(life, FieldLife) and target.matches(life)


def _collect_edits(solver: Solver) -> dict[int, dict[int, OpEdit]]:
    edits: dict[int, dict[int, OpEdit]] = {}
    for elements in solver.timeline.elements.values():
        removed = [e for e in elements if solver.removed(e)]
        if not removed:
            continue
        rec = elements[0].rec
        edit = edits.setdefault(rec.migration.index, {}).setdefault(
            rec.index, OpEdit(rec)
        )
        for e in removed:
            sub = e.sub
            if sub is None:
                edit.remove = True
            elif sub[0] == "field":
                edit.fields.add(sub[1])
            elif sub[0] == "index":
                edit.indexes.add(sub[1])
            elif sub[0] == "constraint":
                edit.constraints.add(sub[1])
            elif sub[0] == "together":
                edit.together.setdefault(sub[1], set()).add(sub[2])
            else:
                edit.entries.add(sub[1])
        if edit.remove:
            edit.fields.clear()
            edit.indexes.clear()
            edit.constraints.clear()
            edit.together.clear()
            edit.entries.clear()
    for seq, op in solver.plan.regenerated.items():
        rec = solver.history.ops[seq]
        edits.setdefault(rec.migration.index, {})[rec.index] = OpEdit(
            rec, replacement=op
        )
    return edits


def _field_names(life: Life) -> set[str]:
    lives = (
        [c for c in life.children if isinstance(c, FieldLife)]
        if isinstance(life, ModelLife)
        else [life]
        if isinstance(life, FieldLife)
        else []
    )
    names: set[str] = set()
    for f in lives:
        assert isinstance(f, FieldLife)
        for n in f.names:
            names.add(n.lower())
            names.add(f"{n.lower()}_id")
        names |= f.extra_names
    return names


def _units(solver: Solver) -> list[UnitInfo]:
    plan = solver.plan
    units: list[UnitInfo] = []

    def add(
        unit: Unit, start: int, end: int | None, life: Life, prefix: str = ""
    ) -> None:
        model = life.model
        units.append(
            UnitInfo(
                key=prefix + solver.unit_key(unit),
                start=start,
                end=end,
                tables=frozenset(
                    {t.lower() for t in model.tables}
                    | {f"{model.app_label}_{n}".lower() for n in model.names}
                ),
                names=frozenset(life.scan_names()),
                at_risk=frozenset(
                    (o.rec.migration.index, o.rec.original_index)
                    for o in solver.at_risk.get(unit, {}).values()
                ),
                app=model.app_label,
                fields=frozenset(_field_names(life)),
                models=frozenset(n.lower() for n in model.names)
                if isinstance(life, ModelLife) and isinstance(unit, Life)
                else frozenset(),
            )
        )

    for life in plan.removed:
        end = life.end_pos
        add(life, life.start_pos[0], end[0] if end else None, life)
    for e1, e2 in plan.superseded:
        assert e1.owner is not None
        add(e1, e1.pos[0], e2.pos[0], e1.owner)
    for f in plan.folds:
        add(f.folded, f.target.pos[0], f.folded.pos[0], f.life, FOLD_PREFIX)
    return units


def _removals(solver: Solver) -> list[Removal]:
    plan = solver.plan
    result: list[Removal] = []
    whole_ops = {
        e.rec.seq
        for elements in solver.timeline.elements.values()
        for e in elements
        if e.sub is None and solver.removed(e)
    }
    for life in sorted(plan.removed, key=lambda x: x.id):
        if life.parent is not None and life.parent in plan.removed:
            continue
        elements = sorted(
            (
                e
                for sub in life.subtree()
                for e in sub.elements
                if e.sub is None or e.rec.seq not in whole_ops
            ),
            key=_sort_key,
        )
        creation, end = life.creation, life.end
        assert creation is not None and end is not None
        result.append(
            Removal(
                kind=life.kind,
                app_label=life.app_label,
                model=life.model.names[-1],
                label=_life_label(life),
                description=_describe_life(life),
                elements=tuple(_location(e) for e in elements),
                name=_entity_name(life),
                start=_mig_name(creation.rec, life.app_label),
                end=_mig_name(end, life.app_label),
            )
        )
    for e1, e2 in plan.superseded:
        owner = e1.owner
        assert owner is not None
        result.append(
            Removal(
                kind="superseded",
                app_label=owner.app_label,
                model=owner.model.names[-1],
                label=_life_label(owner),
                description=(
                    f"{e1.rec.name} in {_mig_name(e1.rec, owner.app_label)}"
                    f" superseded by {e2.rec.name} in"
                    f" {_mig_name(e2.rec, owner.app_label)}"
                ),
                elements=(_location(e1),),
                name=_entity_name(owner)
                if isinstance(owner, FieldLife)
                else e1.rec.name,
                start=_mig_name(e1.rec, owner.app_label),
                end=_mig_name(e2.rec, owner.app_label),
            )
        )
    for e in plan.redundant:
        owner = e.owner
        assert isinstance(owner, ModelLife)
        result.append(
            Removal(
                kind="together",
                app_label=owner.app_label,
                model=owner.names[-1],
                label=owner.label,
                description=(
                    f"{e.rec.name} in {_mig_name(e.rec, owner.app_label)}"
                    " no longer changes anything"
                ),
                elements=(_location(e),),
                name=e.rec.name,
                start=_mig_name(e.rec, owner.app_label),
                note="no longer changes anything",
            )
        )
    for e, name, removed_life in plan.elidable:
        app = e.rec.migration.app_label
        if removed_life is None:
            why = "--elidable"
        else:
            why = f"mentions {name!r}, removed with {_life_label(removed_life)}"
        result.append(
            Removal(
                kind="elidable",
                app_label=app,
                model=None,
                label=app,
                description=f"elidable {e.rec.name} in {e.rec.migration.name} ({why})",
                elements=(_location(e),),
                name=e.rec.name,
                start=e.rec.migration.name,
                note=why,
            )
        )
    for f in plan.folds:
        life, e, p = f.life, f.folded, f.target
        app = life.app_label
        result.append(
            Removal(
                kind="fold",
                app_label=app,
                model=life.model.names[-1],
                label=_life_label(life),
                description=(
                    f"{e.rec.name} in {_mig_name(e.rec, app)} folded into"
                    f" {p.rec.name} in {_mig_name(p.rec, app)}"
                ),
                elements=(_location(e),),
                name=e.rec.name if isinstance(life, ModelLife) else _entity_name(life),
                start=_mig_name(e.rec, app),
                end=_mig_name(p.rec, app),
                targets=(dataclasses.replace(_location(p), detail="regenerated"),),
            )
        )
    result.sort(key=lambda r: (r.app_label, (r.model or "").lower()))
    return result


def _blocked(solver: Solver) -> list[Blocked]:
    plan = solver.plan
    result: list[Blocked] = []
    for life, reasons in plan.blocked.items():
        if life not in plan.roots or life in plan.removed:
            continue
        result.append(
            Blocked(
                kind=life.kind,
                app_label=life.app_label,
                model=life.model.names[-1],
                label=_life_label(life)
                if isinstance(life, (ModelLife, FieldLife))
                else f"{life.model.label} {_describe_short(life)}",
                reasons=tuple(dict.fromkeys(reasons)),
                description=_describe_life(life),
            )
        )
    result.sort(
        key=lambda b: (b.app_label, (b.model or "").lower(), b.label, b.description)
    )
    return result


def _describe_short(life: Life) -> str:
    if isinstance(life, TogetherLife):
        return f"{life.option} ({', '.join(f.names[-1] for f in life.fields)})"
    return f"{life.kind} {life.names[0]!r}"


def load_plan(path: str | os.PathLike[str]) -> Result:
    """Read a plan saved with ``Result.save()``; ``write()`` applies it."""
    from django_clean_migrations.planfile import load_plan as _load

    return _load(Path(path))
