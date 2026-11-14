"""Library entry point: ``clean()`` analyses, ``Result.write()`` applies."""

from __future__ import annotations

import os
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path

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
    verify_in_memory,
    verify_on_disk,
)

__all__ = [
    "Blocked",
    "CleanError",
    "Location",
    "Removal",
    "Result",
    "clean",
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


@dataclass(frozen=True)
class Blocked:
    kind: str
    app_label: str
    model: str | None
    label: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FileChange:
    path: Path
    migration: Key
    module: str
    original: str
    new: str


@dataclass
class Result:
    removals: list[Removal]
    blocked: list[Blocked]
    warnings: list[str]
    files: list[FileChange]
    removed_operations: int
    edited_operations: int
    empty_migrations: list[Key]
    _history: History = field(repr=False)
    _final_state: ProjectState = field(repr=False)
    _expected_ops: dict[Key, list[str]] = field(repr=False)

    @property
    def changed(self) -> bool:
        return bool(self.files)

    def write(self) -> None:
        """
        Write the rewritten files, then reload them from disk and check the
        resulting project state; on any failure restore every file and raise
        CleanError.
        """
        written: list[FileChange] = []
        try:
            for change in self.files:
                _write_text(change.path, change.new)
                written.append(change)
            _forget(self.files)
            verify_on_disk(self._history, self._final_state, self._expected_ops)
        except Exception as exc:
            for change in written:
                _write_text(change.path, change.original)
            _forget(self.files)
            raise CleanError(
                "Verification of the rewritten migration files failed, so they"
                f" were restored: {exc}. Please report this as a bug."
            ) from exc


def _write_text(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


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


def _life_label(life: Life) -> str:
    if isinstance(life, (ModelLife, FieldLife)):
        return life.label
    return life.model.label


def _sort_key(e: Element) -> tuple[int, int]:
    return (e.rec.seq, 0)


def clean(
    targets: Sequence[str] = (),
    *,
    exclude: Sequence[str] = (),
    kinds: Collection[str] | None = None,
    opaque: str = "scan",
    elidable: bool = False,
) -> Result:
    """
    Analyse the whole migration history and plan removals. Nothing is written;
    call ``Result.write()`` to apply. Raises CleanError on invalid options or
    if the planned edit fails verification.
    """
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

    solver = Solver(history, timeline, scope, opaque, elidable)
    solver.solve_dead()
    if elidable:
        solver.drop_all_elidable()
    solver.drop_redundant_together()
    solver.solve_superseded()

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
        _history=history,
        _final_state=timeline.state,
        _expected_ops=expected,
    )


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
        result.append(
            Removal(
                kind=life.kind,
                app_label=life.app_label,
                model=life.model.names[-1],
                label=_life_label(life),
                description=_describe_life(life),
                elements=tuple(_location(e) for e in elements),
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
            )
        )
    for e in plan.elidable:
        app = e.rec.migration.app_label
        result.append(
            Removal(
                kind="elidable",
                app_label=app,
                model=None,
                label=app,
                description=f"elidable {e.rec.name} in {e.rec.migration.name}",
                elements=(_location(e),),
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
            )
        )
    result.sort(key=lambda b: (b.app_label, (b.model or "").lower(), b.label))
    return result


def _describe_short(life: Life) -> str:
    if isinstance(life, TogetherLife):
        return f"{life.option} ({', '.join(f.names[-1] for f in life.fields)})"
    return f"{life.kind} {life.names[0]!r}"
