"""
Library entry point: ``plan()`` analyses, ``Result.save()`` / ``load_plan()``
store and restore a plan, ``Result.write()`` applies it.
"""

from __future__ import annotations

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
    invalidate_modules,
    load_history,
)
from django_clean_migrations.locate import locate_create_model, locate_together_op
from django_clean_migrations.rewrite import OpEdit, edited_operation, rewrite_source
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
    opaque: str = "scan",
    elidable: bool = False,
    trial: bool = False,
    database: str = "default",
    trial_max_runs: int = 8,
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
        targets, exclude=exclude, kinds=kinds, opaque=opaque, elidable=elidable
    )
    if trial:
        from django_clean_migrations.trial import run_trial

        result: Result = run_trial(
            analysis, alias=database, max_runs=trial_max_runs, progress=progress
        )
        return result
    solver = analysis.solve()
    return make_result(analysis, solver, build(analysis, solver))


@dataclass
class Analysis:
    """A loaded and replayed history, ready to be solved repeatedly."""

    history: History
    timeline: Timeline
    scope: Scope
    options: dict[str, Any]

    def solve(
        self,
        *,
        aggressive: bool = False,
        trial_blocked: Mapping[Unit, str] | None = None,
    ) -> Solver:
        solver = Solver(
            self.history,
            self.timeline,
            self.scope,
            self.options["opaque"],
            self.options["elidable"],
            aggressive=aggressive,
            trial_blocked=trial_blocked,
        )
        solver.solve_dead()
        if self.options["elidable"]:
            solver.drop_all_elidable()
        solver.drop_redundant_together()
        solver.solve_superseded()
        return solver


def analyse(
    targets: Sequence[str] = (),
    *,
    exclude: Sequence[str] = (),
    kinds: Collection[str] | None = None,
    opaque: str = "scan",
    elidable: bool = False,
) -> Analysis:
    if opaque not in OPAQUE_POLICIES:
        raise CleanError(f"Unknown opaque policy {opaque!r}.")
    try:
        kind_set = kinds_from(kinds)
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
    """The edits a solved plan makes, as operation lists and per-op edits."""

    edits: dict[int, dict[int, OpEdit]]
    edited_ops: dict[int, list[Operation]]
    """Migration plan index -> its operations after editing."""
    expected: dict[Key, list[str]]

    def operations_by_key(self, history: History) -> dict[Key, list[Operation]]:
        return {history.migrations[i].key: ops for i, ops in self.edited_ops.items()}


def build(analysis: Analysis, solver: Solver) -> Built:
    """Edited operation lists, verified in memory against the original."""
    history, timeline = analysis.history, analysis.timeline
    edits = _collect_edits(solver)
    edited_ops: dict[int, list[Operation]] = {}
    expected: dict[Key, list[str]] = {}
    for info_index, op_edits in edits.items():
        info = history.migrations[info_index]
        new_ops: list[Operation] = []
        for rec_index, op in enumerate(info.migration.operations):
            edit = op_edits.get(rec_index)
            new_op = edited_operation(edit) if edit is not None else op
            if new_op is not None:
                new_ops.append(new_op)
        edited_ops[info_index] = new_ops
        expected[info.key] = [type(op).__name__ for op in new_ops]
    try:
        verify_in_memory(history, edited_ops, timeline.dangling, timeline.state)
    except VerificationError as exc:
        raise CleanError(
            f"Internal error: {exc}. No files were changed. Please report this as a"
            " bug, and use --exclude to work around it."
        ) from exc
    return Built(edits, edited_ops, expected)


def make_result(analysis: Analysis, solver: Solver, built: Built) -> Result:
    history, timeline = analysis.history, analysis.timeline
    edits, edited_ops, expected = built.edits, built.edited_ops, built.expected
    files: list[FileChange] = []
    removed_count = 0
    edited_count = 0
    empty: list[Key] = []
    for info_index, op_edits in sorted(edits.items()):
        info = history.migrations[info_index]
        assert info.source is not None and info.path is not None
        new_text = rewrite_source(info.source, list(op_edits.values()))
        try:
            compile(new_text, str(info.path), "exec")
        except SyntaxError as exc:
            raise CleanError(
                f"Internal error: rewriting {info.label} produced invalid Python"
                f" ({exc}). No files were changed. Please report this as a bug."
            ) from exc
        files.append(
            FileChange(
                info.path,
                info.key,
                type(info.migration).__module__,
                info.source.text,
                new_text,
                tuple(expected[info.key]),
            )
        )
        removed_count += sum(1 for e in op_edits.values() if e.remove)
        edited_count += sum(
            1 for e in op_edits.values() if not e.remove and not e.empty
        )
        if not edited_ops[info_index]:
            empty.append(info.key)

    return Result(
        removals=_removals(solver),
        blocked=_blocked(solver),
        warnings=list(history.warnings),
        files=files,
        removed_operations=removed_count,
        edited_operations=edited_count,
        empty_migrations=empty,
        options=dict(analysis.options),
        removed_positions=frozenset(
            (history.migrations[m].key, i)
            for m, op_edits in edits.items()
            for i, e in op_edits.items()
            if e.remove
        ),
        edited_positions=frozenset(
            (history.migrations[m].key, i)
            for m, op_edits in edits.items()
            for i, e in op_edits.items()
            if not e.remove and not e.empty
        ),
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
    return edits


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
