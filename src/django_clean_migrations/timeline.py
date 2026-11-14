"""
Replay of the whole history, tracking entities by identity ("lives") rather
than by name, the source elements that belong to them, and the references
between elements and lives.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from django.db import models
from django.db.migrations import operations as m
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ModelState, ProjectState
from django.db.migrations.utils import resolve_relation
from django.db.models.expressions import F
from django.db.models.query_utils import Q

from django_clean_migrations.history import History, HistoryError, Key, OpRecord
from django_clean_migrations.locate import (
    TOGETHER_OPTIONS,
    Entry,
    check_sub_elements,
    normalize_together,
)
from django_clean_migrations.opaque import OpaqueOp, Pos
from django_clean_migrations.ops import INERT, OPAQUE, classify
from django_clean_migrations.verify import DanglingTracker

_ids = itertools.count()


class Life:
    kind: ClassVar[str]

    def __init__(self, parent: ModelLife | None, name: str) -> None:
        self.id = next(_ids)
        self.parent = parent
        self.names: list[str] = [name]
        self.creation: Element | None = None
        self.end: OpRecord | None = None
        self.elements: list[Element] = []
        self.poison_reasons: list[str] = []
        self.referenced_by: list[Element] = []
        self.soft_referenced_by: list[Element] = []

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.label}>"

    @property
    def model(self) -> ModelLife:
        assert self.parent is not None
        return self.parent

    @property
    def app_label(self) -> str:
        return self.model.app_label

    @property
    def label(self) -> str:
        return f"{self.model.label}.{self.names[-1]}"

    @property
    def poisoned(self) -> bool:
        return bool(self.poison_reasons)

    def poison(self, reason: str) -> None:
        if reason not in self.poison_reasons:
            self.poison_reasons.append(reason)
        if self.parent is not None:
            self.parent.poison(reason)

    @property
    def start_pos(self) -> Pos:
        assert self.creation is not None
        return self.creation.pos

    @property
    def end_pos(self) -> Pos | None:
        if self.end is None:
            return None
        return (self.end.migration.index, self.end.index)

    @property
    def is_candidate(self) -> bool:
        # Poisoned lives stay candidates so they are reported as blocked.
        return self.creation is not None and self.end is not None

    def scan_names(self) -> set[str]:
        return {n.lower() for n in self.names}

    def subtree(self) -> Iterator[Life]:
        yield self


class ModelLife(Life):
    kind = "model"

    def __init__(self, app_label: str, name: str) -> None:
        super().__init__(None, name)
        self._app_label = app_label
        self.fields: dict[str, FieldLife] = {}
        self.indexes: dict[str, Life] = {}
        self.constraints: dict[str, ConstraintLife] = {}
        self.together: dict[str, dict[tuple[FieldLife, ...], TogetherLife]] = {
            o: {} for o in TOGETHER_OPTIONS
        }
        self.together_events: dict[str, list[TogetherEvent]] = {
            o: [] for o in TOGETHER_OPTIONS
        }
        self.children: list[Life] = []
        self.tables: set[str] = set()
        self.default_related_names: set[str] = set()

    def note_options(self, options: Any) -> None:
        table = options.get("db_table")
        if isinstance(table, str):
            self.tables.add(table)
        related = options.get("default_related_name")
        if isinstance(related, str):
            self.default_related_names.add(related)

    @property
    def app_label(self) -> str:
        return self._app_label

    @property
    def model(self) -> ModelLife:
        return self

    @property
    def label(self) -> str:
        return f"{self._app_label}.{self.names[-1]}"

    @property
    def key(self) -> Key:
        return (self._app_label, self.names[-1].lower())

    def scan_names(self) -> set[str]:
        names = super().scan_names() | {t.lower() for t in self.tables}
        for child in self.children:
            if isinstance(child, FieldLife):
                names |= child.related_names
        return names

    def subtree(self) -> Iterator[Life]:
        yield self
        yield from self.children


class FieldLife(Life):
    kind = "field"

    def __init__(self, parent: ModelLife, name: str) -> None:
        super().__init__(parent, name)
        self.defs: list[tuple[Element, models.Field[Any, Any], Life | None]] = []
        """(element, definition, remote model life) in plan order."""
        self.extra_names: set[str] = set()
        self.related_names: set[str] = set()
        self.ever_pk = False

    @property
    def current(self) -> models.Field[Any, Any] | None:
        return self.defs[-1][1] if self.defs else None

    def scan_names(self) -> set[str]:
        names = super().scan_names() | self.extra_names | self.related_names
        if any(getattr(d, "remote_field", None) is not None for _, d, _ in self.defs):
            # Default reverse names: the owning model's name (query name,
            # <model>_set accessor) and its default_related_name.
            model = self.model
            names |= {n.lower() for n in model.names}
            for value in model.default_related_names:
                name = _related_name(value, model)
                if name:
                    names.add(name)
        return names


class IndexLife(Life):
    kind = "index"


class ConstraintLife(Life):
    kind = "constraint"


class TogetherLife(Life):
    """
    An entry of unique_together / index_together. RenameIndex(old_fields=...)
    turns an index_together entry into a named index: the same life continues.
    """

    kind = "together"

    def __init__(
        self, parent: ModelLife, option: str, fields: tuple[FieldLife, ...]
    ) -> None:
        super().__init__(parent, "")
        self.names = []
        self.option = option
        self.fields = fields

    @property
    def label(self) -> str:
        names = ", ".join(f.names[-1] for f in self.fields)
        return f"{self.model.label} {self.option} ({names})"


@dataclass(eq=False)
class Element:
    rec: OpRecord
    sub: tuple[Any, ...] | None
    owner: Life | None
    opaque: bool = False
    refs: list[Life] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)
    _reason: Any = field(default=False, repr=False)

    @property
    def pos(self) -> Pos:
        return (self.rec.migration.index, self.rec.index)

    def not_editable_reason(self) -> str | None:
        if self._reason is False:
            if self.opaque:
                self._reason = f"{self.rec.describe()} is an opaque operation"
            elif self.sub is None:
                self._reason = self.rec.not_editable_reason()
            else:
                self._reason = check_sub_elements(self.rec, [self.sub])
        return self._reason  # type: ignore[no-any-return]

    def describe(self) -> str:
        if self.sub is None:
            return self.rec.describe()
        return f"{self.rec.describe()} ({describe_sub(self.sub)})"


def describe_sub(sub: tuple[Any, ...]) -> str:
    kind = sub[0]
    if kind == "field":
        return f"field {sub[1]!r}"
    if kind in ("index", "constraint"):
        return f"{kind} #{sub[1] + 1}"
    if kind == "together":
        return f"{sub[1]} entry {sub[2]!r}"
    return f"entry {sub[1]!r}"


@dataclass(eq=False)
class TogetherEvent:
    rec: OpRecord
    kind: str
    """"create" (CreateModel option), "alter" (AlterXTogether), "rename_index"."""
    lives: list[TogetherLife]
    element: Element | None


def mentioned_names(obj: Any) -> set[str]:
    """
    Every identifier-like string reachable from an index/constraint/expression.
    Deliberately over-inclusive: a false positive only blocks a removal.
    """
    out: set[str] = set()
    seen: set[int] = set()
    stack = [obj]
    while stack:
        o = stack.pop()
        if isinstance(o, str):
            for part in re.split(r"__|\W+", o):
                if part:
                    out.add(part)
                    if part.endswith("_id"):
                        out.add(part[:-3])
            continue
        if o is None or isinstance(o, (int, float, bool, type)) or id(o) in seen:
            continue
        seen.add(id(o))
        if isinstance(o, (list, tuple, set, frozenset)):
            stack.extend(o)
        elif isinstance(o, dict):
            stack.extend(o.keys())
            stack.extend(o.values())
        elif isinstance(o, F):
            stack.append(getattr(o, "name", None))
        elif isinstance(o, Q):
            stack.extend(o.children)
        else:
            if hasattr(o, "deconstruct"):
                try:
                    parts = o.deconstruct()
                except Exception:
                    parts = ()
                if isinstance(parts, tuple) and len(parts) >= 2:
                    stack.append(parts[-2])
                    stack.append(parts[-1])
            if hasattr(o, "get_source_expressions"):
                try:
                    stack.extend(o.get_source_expressions())
                except Exception:
                    pass
    return out


def _is_expression(value: Any) -> bool:
    return hasattr(value, "resolve_expression") and not isinstance(value, type)


def _resolve(model: Any, app_label: str, model_name: str) -> Key | None:
    try:
        app, name = resolve_relation(model, app_label, model_name)
    except (TypeError, ValueError, AttributeError):
        return None
    return (app, name.lower())


def _related_name(value: Any, model: ModelLife) -> str | None:
    if not isinstance(value, str) or not value or value.endswith("+"):
        return None
    name = model.names[-1].lower()
    return (
        value.replace("%(class)s", name)
        .replace("%(model_name)s", name)
        .replace("%(app_label)s", model.app_label)
        .lower()
    )


@dataclass(eq=False)
class _ModelSnap:
    state: ModelState
    name: str
    fields: dict[str, tuple[Any, Any]]
    options: dict[str, Any]
    bases: Any
    managers: list[Any]


def _fingerprint(f: Any) -> Any:
    # ProjectState.rename_field/rename_model mutate relation attributes in place.
    remote = getattr(f, "remote_field", None)
    if remote is None:
        return None
    return (
        repr(getattr(f, "to_fields", None)),
        repr(getattr(f, "from_fields", None)),
        repr(getattr(remote, "model", None)),
        repr(getattr(remote, "through", None)),
        getattr(remote, "field_name", None),
        repr(getattr(f, "field_names", None)),
    )


def _snapshot(state: ProjectState) -> dict[Key, _ModelSnap]:
    return {
        key: _ModelSnap(
            ms,
            ms.name,
            {n: (f, _fingerprint(f)) for n, f in ms.fields.items()},
            dict(ms.options),
            ms.bases,
            list(ms.managers),
        )
        for key, ms in state.models.items()
    }


def _same(a: Any, b: Any) -> bool:
    if a is b:
        return True
    try:
        return bool(a == b)
    except Exception:
        return False


class Timeline:
    def __init__(self, history: History) -> None:
        self.history = history
        self.state = ProjectState(real_apps=set(history.loader.unmigrated_apps))
        self.models: dict[Key, ModelLife] = {}
        self.lives: list[Life] = []
        self.elements: dict[int, list[Element]] = {}
        self.opaque: list[OpaqueOp] = []
        self.targets_of: dict[FieldLife, set[Key]] = {}
        self.by_target: dict[Key, set[FieldLife]] = {}
        self.tracker = DanglingTracker()
        self.dangling: dict[int, frozenset[Key]] = {}

    # Replay

    def replay(self) -> None:
        ops = iter(self.history.ops)
        rec: OpRecord | None = next(ops, None)
        for info in self.history.migrations:
            while rec is not None and rec.migration is info:
                touched = self.tracker.before(rec.op, info.app_label, self.state)
                self._apply(rec)
                self.tracker.after(touched, self.state)
                rec = next(ops, None)
            if self.tracker.missing:
                self.dangling[info.index] = frozenset(self.tracker.missing)

    def _state_forwards(self, op: Operation, app_label: str, rec: OpRecord) -> None:
        try:
            op.state_forwards(app_label, self.state)
        except Exception as exc:
            raise HistoryError(
                f"Could not apply {type(op).__name__} (operation"
                f" {rec.index + 1} of {rec.migration.label}) to the project"
                f" state: {exc!r}"
            ) from exc

    def _apply(self, rec: OpRecord) -> None:
        op = rec.op
        app_label = rec.migration.app_label
        kind = classify(op)
        if kind == OPAQUE:
            element = self._register(Element(rec, None, None, opaque=True))
            changed = self._opaque_state(op, app_label, rec, element)
            self.opaque.append(OpaqueOp(rec, changed))
            return
        if kind == INERT:
            self._register(Element(rec, None, None))
        else:
            getattr(self, f"_on_{kind}")(rec, op, app_label, None)
        self._state_forwards(op, app_label, rec)

    def _opaque_state(
        self, op: Operation, app_label: str, rec: OpRecord, ctx: Element
    ) -> bool:
        cls = type(op)
        if cls is m.RunPython:
            return False
        if cls is m.RunSQL or cls is m.SeparateDatabaseAndState:
            changed = False
            for inner in op.state_operations:  # type: ignore[attr-defined]
                kind = classify(inner)
                if kind == OPAQUE:
                    changed |= self._opaque_state(inner, app_label, rec, ctx)
                elif kind == INERT:
                    self._state_forwards(inner, app_label, rec)
                else:
                    getattr(self, f"_on_{kind}")(rec, inner, app_label, ctx)
                    self._state_forwards(inner, app_label, rec)
                    changed |= kind != "ValidateConstraint"
            return changed
        before = _snapshot(self.state)
        self._state_forwards(op, app_label, rec)
        return self._resync(before, _snapshot(self.state), ctx)

    # Helpers

    def _register(self, element: Element) -> Element:
        self.elements.setdefault(element.rec.seq, []).append(element)
        if element.owner is not None:
            element.owner.elements.append(element)
        return element

    def _element(
        self,
        rec: OpRecord,
        sub: tuple[Any, ...] | None,
        owner: Life | None,
        ctx: Element | None,
    ) -> Element:
        if ctx is not None:
            if owner is not None:
                owner.poison(f"changed by {ctx.rec.describe()}")
            return ctx
        return self._register(Element(rec, sub, owner))

    def _new_life(self, life: Life, creation: Element) -> Life:
        life.creation = creation
        self.lives.append(life)
        if life.parent is not None:
            life.parent.children.append(life)
        if creation.opaque:
            life.poison(f"created by {creation.rec.describe()}")
        return life

    @staticmethod
    def _ref(element: Element, life: Life | None, soft: bool = False) -> None:
        if life is None:
            return
        if soft:
            life.soft_referenced_by.append(element)
        else:
            element.refs.append(life)
            life.referenced_by.append(element)

    def _model(
        self, app_label: str, name: str, rec: OpRecord, ctx: Element | None
    ) -> ModelLife | None:
        life = self.models.get((app_label, name.lower()))
        if life is None and ctx is None:
            # Inconsistent history: keep the operation, attached to nothing.
            self._register(Element(rec, None, None))
        return life

    def _pk(self, model: ModelLife) -> FieldLife | None:
        for f in model.fields.values():
            current = f.current
            if current is not None and getattr(current, "primary_key", False):
                return f
        return None

    def _set_targets(self, life: FieldLife, targets: set[Key]) -> None:
        for old in self.targets_of.pop(life, set()):
            self.by_target.get(old, set()).discard(life)
        if targets:
            self.targets_of[life] = targets
            for t in targets:
                self.by_target.setdefault(t, set()).add(life)

    def _field_def(
        self,
        element: Element,
        model: ModelLife,
        life: FieldLife,
        fld: models.Field[Any, Any],
    ) -> None:
        """Record a field definition carried by ``element``."""
        life.ever_pk |= bool(getattr(fld, "primary_key", False))
        db_column = getattr(fld, "db_column", None)
        if isinstance(db_column, str):
            life.extra_names.add(db_column.lower())
        app_label, model_name = model.app_label, model.names[-1].lower()
        targets: set[Key] = set()
        remote_life: Life | None = None
        remote = getattr(fld, "remote_field", None)
        if remote is not None:
            for attr in ("related_name", "related_query_name"):
                rn = _related_name(getattr(remote, attr, None), model)
                if rn:
                    life.related_names.add(rn)
            db_table = getattr(fld, "db_table", None)
            if isinstance(db_table, str):
                life.extra_names.add(db_table.lower())
            to_key = _resolve(remote.model, app_label, model_name)
            if to_key is not None:
                targets.add(to_key)
                target = self.models.get(to_key)
                remote_life = target
                self._ref(element, target)
                if target is not None:
                    to_fields = getattr(fld, "to_fields", None)
                    if not to_fields or None in to_fields:
                        self._ref(element, self._pk(target))
                    for name in to_fields or ():
                        if name is not None:
                            self._ref(element, target.fields.get(name))
            through = getattr(remote, "through", None)
            if through is not None:
                through_key = _resolve(through, app_label, model_name)
                if through_key is not None:
                    targets.add(through_key)
                    through_life = self.models.get(through_key)
                    self._ref(element, through_life)
                    if through_life is not None:
                        for name in getattr(remote, "through_fields", None) or ():
                            self._ref(element, through_life.fields.get(name))
        local: set[str] = set()
        for attr in ("from_fields", "field_names"):
            local.update(
                n for n in getattr(fld, attr, None) or () if isinstance(n, str)
            )
        try:
            kwargs = fld.deconstruct()[3]
        except Exception:
            kwargs = {}
        for value in kwargs.values():
            if _is_expression(value):
                local |= mentioned_names(value)
        for name in local:
            if name != "self":
                self._ref_local(element, model, name)
        self._set_targets(life, targets)
        life.defs.append((element, fld, remote_life))

    def _ref_local(
        self, element: Element, model: ModelLife, name: str, soft: bool = False
    ) -> None:
        target = model.fields.get(name)
        if target is not None:
            self._ref(element, target, soft)

    def _refs_mentioned(self, element: Element, model: ModelLife, obj: Any) -> None:
        for name in mentioned_names(obj):
            self._ref_local(element, model, name)

    def _soft_refs(self, element: Element, model: ModelLife, options: Any) -> None:
        values: list[Any] = []
        ordering = options.get("ordering")
        if isinstance(ordering, (list, tuple)):
            values.extend(ordering)
        latest = options.get("get_latest_by")
        if isinstance(latest, str):
            values.append(latest)
        elif isinstance(latest, (list, tuple)):
            values.extend(latest)
        for value in values:
            if isinstance(value, str):
                name = value.lstrip("-?").split("__")[0]
                self._ref_local(element, model, name, soft=True)
            else:
                for name in mentioned_names(value):
                    self._ref_local(element, model, name, soft=True)

    def _end_field(self, life: FieldLife, rec: OpRecord) -> None:
        life.end = rec
        self._set_targets(life, set())

    # Known operations

    def _on_CreateModel(
        self, rec: OpRecord, op: m.CreateModel, app_label: str, ctx: Element | None
    ) -> None:
        key = (app_label, op.name_lower)
        old = self.models.get(key)
        model = ModelLife(app_label, op.name)
        element = self._element(rec, None, None, ctx)
        self._attach(element, model, ctx)
        self._new_life(model, element)
        if old is not None:
            old.poison(f"model name reused without DeleteModel in {rec.describe()}")
            model.poison(f"model name reused without DeleteModel in {rec.describe()}")
        self.models[key] = model
        model.note_options(op.options)
        for name, _ in op.fields:
            life = FieldLife(model, name)
            sub_el = self._element(rec, ("field", name), None, ctx)
            self._attach(sub_el, life, ctx)
            self._new_life(life, sub_el)
            model.fields[name] = life
        for (_, fld), life in zip(op.fields, list(model.fields.values()), strict=True):
            assert life.creation is not None
            self._field_def(life.creation, model, life, fld)
        for i, index in enumerate(op.options.get("indexes", ())):
            ilife = IndexLife(model, index.name)
            sub_el = self._element(rec, ("index", i), None, ctx)
            self._attach(sub_el, ilife, ctx)
            self._new_life(ilife, sub_el)
            model.indexes[index.name] = ilife
            self._refs_mentioned(sub_el, model, index)
        for i, constraint in enumerate(op.options.get("constraints", ())):
            clife = ConstraintLife(model, constraint.name)
            sub_el = self._element(rec, ("constraint", i), None, ctx)
            self._attach(sub_el, clife, ctx)
            self._new_life(clife, sub_el)
            model.constraints[constraint.name] = clife
            self._refs_mentioned(sub_el, model, constraint)
        for option in TOGETHER_OPTIONS:
            lives: list[TogetherLife] = []
            for entry in normalize_together(op.options.get(option)):
                flives = self._field_lives(model, entry)
                if flives is None:
                    model.poison(f"{option} names unknown fields in {rec.describe()}")
                    continue
                if flives in model.together[option]:
                    continue
                tlife = TogetherLife(model, option, flives)
                sub_el = self._element(rec, ("together", option, entry), None, ctx)
                self._attach(sub_el, tlife, ctx)
                self._new_life(tlife, sub_el)
                for f in flives:
                    self._ref(sub_el, f)
                model.together[option][flives] = tlife
                lives.append(tlife)
            if lives or option in op.options:
                model.together_events[option].append(
                    TogetherEvent(rec, "create", lives, None if ctx else element)
                )
        wrt = op.options.get("order_with_respect_to")
        if isinstance(wrt, str):
            self._ref_local(element, model, wrt)
        self._soft_refs(element, model, op.options)
        for base in op.bases or ():
            if isinstance(base, str) or hasattr(base, "_meta"):
                base_key = _resolve(base, app_label, op.name_lower)
                if base_key is not None:
                    self._ref(element, self.models.get(base_key))
        # Fields that already point at this name (dangling until now) now
        # reference this life for as long as they exist.
        for f in list(self.by_target.get(key, ())):
            if f.creation is not None and f.parent is not model:
                self._ref(f.creation, model)
                pk = self._pk(model)
                if pk is not None:
                    self._ref(f.creation, pk)

    def _attach(self, element: Element, life: Life, ctx: Element | None) -> None:
        if ctx is None:
            element.owner = life
            life.elements.append(element)

    def _field_lives(
        self, model: ModelLife, entry: Entry
    ) -> tuple[FieldLife, ...] | None:
        lives = tuple(model.fields.get(n) for n in entry)
        if any(f is None for f in lives):
            return None
        return tuple(f for f in lives if f is not None)

    def _on_DeleteModel(
        self, rec: OpRecord, op: m.DeleteModel, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.name, rec, ctx)
        if model is None:
            return
        self._element(rec, None, model, ctx)
        model.end = rec
        del self.models[model.key]
        for f in model.fields.values():
            self._set_targets(f, set())

    def _on_RenameModel(
        self, rec: OpRecord, op: m.RenameModel, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.old_name, rec, ctx)
        if model is None:
            return
        self._element(rec, None, model, ctx)
        old_key = model.key
        del self.models[old_key]
        model.names.append(op.new_name)
        new_key = model.key
        if new_key in self.models:
            self.models[new_key].poison(f"name taken over in {rec.describe()}")
            model.poison(f"name taken over in {rec.describe()}")
        self.models[new_key] = model
        for f in self.by_target.pop(old_key, set()):
            targets = self.targets_of[f]
            targets.discard(old_key)
            targets.add(new_key)
            self.by_target.setdefault(new_key, set()).add(f)

    def _model_op(
        self, rec: OpRecord, op: Any, app_label: str, ctx: Element | None
    ) -> tuple[ModelLife | None, Element | None]:
        model = self._model(app_label, op.name, rec, ctx)
        if model is None:
            return None, None
        return model, self._element(rec, None, model, ctx)

    def _on_AlterModelTable(
        self, rec: OpRecord, op: m.AlterModelTable, app_label: str, ctx: Element | None
    ) -> None:
        model, _ = self._model_op(rec, op, app_label, ctx)
        if model is not None and isinstance(op.table, str):
            model.tables.add(op.table)

    def _on_AlterModelTableComment(
        self,
        rec: OpRecord,
        op: m.AlterModelTableComment,
        app_label: str,
        ctx: Element | None,
    ) -> None:
        self._model_op(rec, op, app_label, ctx)

    def _on_AlterModelOptions(
        self,
        rec: OpRecord,
        op: m.AlterModelOptions,
        app_label: str,
        ctx: Element | None,
    ) -> None:
        model, element = self._model_op(rec, op, app_label, ctx)
        if model is None or element is None:
            return
        ms = self.state.models.get(model.key)
        if ms is not None and element is not ctx:
            element.info["managed_before"] = ms.options.get("managed", True)
        model.note_options(op.options)
        self._soft_refs(element, model, op.options)

    def _on_AlterModelManagers(
        self,
        rec: OpRecord,
        op: m.AlterModelManagers,
        app_label: str,
        ctx: Element | None,
    ) -> None:
        self._model_op(rec, op, app_label, ctx)

    def _on_AlterOrderWithRespectTo(
        self,
        rec: OpRecord,
        op: m.AlterOrderWithRespectTo,
        app_label: str,
        ctx: Element | None,
    ) -> None:
        model, element = self._model_op(rec, op, app_label, ctx)
        if model is not None and element is not None and op.order_with_respect_to:
            self._ref_local(element, model, op.order_with_respect_to)

    def _on_AlterUniqueTogether(
        self,
        rec: OpRecord,
        op: m.AlterUniqueTogether | m.AlterIndexTogether,
        app_label: str,
        ctx: Element | None,
    ) -> None:
        model, element = self._model_op(rec, op, app_label, ctx)
        if model is None:
            return
        option = op.option_name
        assert option is not None
        resolved: list[tuple[Entry, tuple[FieldLife, ...]]] = []
        for entry in normalize_together(op.option_value):
            flives = self._field_lives(model, entry)
            if flives is None:
                model.poison(f"{option} names unknown fields in {rec.describe()}")
                for t in model.together[option].values():
                    t.poison(f"{option} could not be tracked in {rec.describe()}")
                continue
            resolved.append((entry, flives))
        current = model.together[option]
        new: dict[tuple[FieldLife, ...], TogetherLife] = {}
        lives: list[TogetherLife] = []
        for entry, flives in resolved:
            if flives in new:
                continue
            tlife = current.get(flives)
            sub_el = self._element(rec, ("entry", entry), None, ctx)
            if tlife is None:
                tlife = TogetherLife(model, option, flives)
                self._attach(sub_el, tlife, ctx)
                self._new_life(tlife, sub_el)
            else:
                self._attach(sub_el, tlife, ctx)
                if ctx is not None:
                    tlife.poison(f"changed by {ctx.rec.describe()}")
            for f in flives:
                self._ref(sub_el, f)
            new[flives] = tlife
            lives.append(tlife)
        for flives, tlife in current.items():
            if flives not in new:
                tlife.end = rec
                if ctx is not None:
                    tlife.poison(f"changed by {ctx.rec.describe()}")
        model.together[option] = new
        model.together_events[option].append(
            TogetherEvent(rec, "alter", lives, None if ctx else element)
        )

    _on_AlterIndexTogether = _on_AlterUniqueTogether

    def _field(
        self, model: ModelLife, name: str, rec: OpRecord, ctx: Element | None
    ) -> FieldLife | None:
        life = model.fields.get(name)
        if life is None:
            model.poison(f"unknown field {name!r} in {rec.describe()}")
            if ctx is None:
                self._register(Element(rec, None, model))
        return life

    def _on_AddField(
        self, rec: OpRecord, op: m.AddField, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        if model is None:
            return
        life = FieldLife(model, op.name)
        old = model.fields.get(op.name)
        if old is not None:
            old.poison(f"field name reused without RemoveField in {rec.describe()}")
            life.poison(f"field name reused without RemoveField in {rec.describe()}")
        element = self._element(rec, None, None, ctx)
        self._attach(element, life, ctx)
        self._new_life(life, element)
        model.fields[op.name] = life
        self._field_def(element, model, life, op.field)

    def _on_RemoveField(
        self, rec: OpRecord, op: m.RemoveField, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        if model is None:
            return
        life = self._field(model, op.name, rec, ctx)
        if life is None:
            return
        self._element(rec, None, life, ctx)
        del model.fields[op.name]
        self._end_field(life, rec)

    def _on_AlterField(
        self, rec: OpRecord, op: m.AlterField, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        if model is None:
            return
        life = self._field(model, op.name, rec, ctx)
        if life is None:
            return
        element = self._element(rec, None, life, ctx)
        self._field_def(element, model, life, op.field)

    def _on_RenameField(
        self, rec: OpRecord, op: m.RenameField, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        if model is None:
            return
        life = self._field(model, op.old_name, rec, ctx)
        if life is None:
            return
        element = self._element(rec, None, life, ctx)
        if element is not ctx:
            element.info["rename"] = True
        del model.fields[op.old_name]
        if op.new_name in model.fields:
            model.fields[op.new_name].poison(f"name taken over in {rec.describe()}")
            life.poison(f"name taken over in {rec.describe()}")
        model.fields[op.new_name] = life
        life.names.append(op.new_name)

    def _on_AddIndex(
        self, rec: OpRecord, op: m.AddIndex, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        if model is None:
            return
        life = IndexLife(model, op.index.name)
        element = self._element(rec, None, None, ctx)
        self._attach(element, life, ctx)
        self._new_life(life, element)
        if op.index.name in model.indexes:
            model.indexes[op.index.name].poison(
                f"index name reused in {rec.describe()}"
            )
            life.poison(f"index name reused in {rec.describe()}")
        model.indexes[op.index.name] = life
        self._refs_mentioned(element, model, op.index)

    def _on_RemoveIndex(
        self, rec: OpRecord, op: m.RemoveIndex, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        if model is None:
            return
        life = model.indexes.pop(op.name, None)
        if life is None:
            model.poison(f"unknown index {op.name!r} in {rec.describe()}")
            self._element(rec, None, model, ctx)
            return
        self._element(rec, None, life, ctx)
        life.end = rec

    def _on_RenameIndex(
        self, rec: OpRecord, op: m.RenameIndex, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        if model is None:
            return
        life: Life | None
        if op.old_fields:
            flives = self._field_lives(model, tuple(op.old_fields))
            tlife = (
                model.together["index_together"].pop(flives, None)
                if flives is not None
                else None
            )
            if tlife is not None:
                element = self._element(rec, None, tlife, ctx)
                tlife.names.append(op.new_name)
                model.together_events["index_together"].append(
                    TogetherEvent(rec, "rename_index", [tlife], None)
                )
                life = tlife
            else:
                life = IndexLife(model, op.new_name)
                element = self._element(rec, None, None, ctx)
                self._attach(element, life, ctx)
                self._new_life(life, element)
                life.poison(f"index_together entry not found in {rec.describe()}")
            for name in op.old_fields:
                self._ref_local(element, model, name)
        else:
            assert op.old_name is not None
            life = model.indexes.pop(op.old_name, None)
            if life is None:
                model.poison(f"unknown index {op.old_name!r} in {rec.describe()}")
                self._element(rec, None, model, ctx)
                return
            self._element(rec, None, life, ctx)
            life.names.append(op.new_name)
        model.indexes[op.new_name] = life

    def _on_AddConstraint(
        self, rec: OpRecord, op: m.AddConstraint, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        if model is None:
            return
        name = op.constraint.name
        life = ConstraintLife(model, name)
        element = self._element(rec, None, None, ctx)
        self._attach(element, life, ctx)
        self._new_life(life, element)
        if name in model.constraints:
            model.constraints[name].poison(
                f"constraint name reused in {rec.describe()}"
            )
            life.poison(f"constraint name reused in {rec.describe()}")
        model.constraints[name] = life
        self._refs_mentioned(element, model, op.constraint)

    def _on_RemoveConstraint(
        self,
        rec: OpRecord,
        op: m.RemoveConstraint,
        app_label: str,
        ctx: Element | None,
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        if model is None:
            return
        life = model.constraints.pop(op.name, None)
        if life is None:
            model.poison(f"unknown constraint {op.name!r} in {rec.describe()}")
            self._element(rec, None, model, ctx)
            return
        self._element(rec, None, life, ctx)
        life.end = rec

    def _on_AlterConstraint(
        self,
        rec: OpRecord,
        op: Any,
        app_label: str,
        ctx: Element | None,
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        if model is None:
            return
        life = model.constraints.get(op.name)
        if life is None:
            model.poison(f"unknown constraint {op.name!r} in {rec.describe()}")
            self._element(rec, None, model, ctx)
            return
        element = self._element(rec, None, life, ctx)
        self._refs_mentioned(element, model, op.constraint)
        new_name = getattr(op.constraint, "name", op.name)
        if new_name != op.name:
            del model.constraints[op.name]
            model.constraints[new_name] = life
            life.names.append(new_name)

    def _on_ValidateConstraint(
        self, rec: OpRecord, op: Any, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        if model is None:
            return
        life = model.constraints.get(op.name)
        self._element(rec, None, life if life is not None else model, ctx)

    # Opaque state changes

    def _resync(
        self, before: dict[Key, _ModelSnap], after: dict[Key, _ModelSnap], ctx: Element
    ) -> bool:
        changed = False
        reason = f"changed by {ctx.rec.describe()}"
        for key in before.keys() - after.keys():
            changed = True
            model = self.models.pop(key, None)
            if model is not None:
                model.end = ctx.rec
                model.poison(reason)
                for f in model.fields.values():
                    self._set_targets(f, set())
        for key in after.keys() - before.keys():
            changed = True
            self._opaque_model(key, after[key], ctx)
        for key in before.keys() & after.keys():
            b, a = before[key], after[key]
            if self._snap_equal(b, a):
                continue
            changed = True
            model = self.models.get(key)
            if model is None:
                self._opaque_model(key, a, ctx)
                continue
            model.poison(reason)
            if a.name != model.names[-1]:
                model.names.append(a.name)
            self._resync_fields(model, b, a, ctx)
            self._resync_named(model, model.indexes, b, a, "indexes", IndexLife, ctx)
            self._resync_named(
                model,
                model.constraints,  # type: ignore[arg-type]
                b,
                a,
                "constraints",
                ConstraintLife,
                ctx,
            )
            for option in TOGETHER_OPTIONS:
                if not _same(b.options.get(option), a.options.get(option)):
                    self._resync_together(model, option, a.options.get(option), ctx)
            model.note_options(a.options)
        return changed

    @staticmethod
    def _snap_equal(b: _ModelSnap, a: _ModelSnap) -> bool:
        if b.name != a.name or not _same(b.bases, a.bases):
            return False
        if b.fields.keys() != a.fields.keys() or any(
            b.fields[n][0] is not a.fields[n][0] or b.fields[n][1] != a.fields[n][1]
            for n in b.fields
        ):
            return False
        if b.options.keys() != a.options.keys() or any(
            not _same(b.options[k], a.options[k]) for k in b.options
        ):
            return False
        return len(b.managers) == len(a.managers) and all(
            x is y or _same(x, y) for x, y in zip(b.managers, a.managers, strict=True)
        )

    def _opaque_model(self, key: Key, snap: _ModelSnap, ctx: Element) -> None:
        old = self.models.get(key)
        if old is not None:
            old.poison(f"replaced by {ctx.rec.describe()}")
            old.end = ctx.rec
        model = ModelLife(key[0], snap.name)
        self._new_life(model, ctx)
        self.models[key] = model
        model.note_options(snap.options)
        for name in snap.fields:
            life = FieldLife(model, name)
            self._new_life(life, ctx)
            model.fields[name] = life
        for name, (fld, _) in snap.fields.items():
            self._field_def(ctx, model, model.fields[name], fld)
        for index in snap.options.get("indexes", ()):
            model.indexes[index.name] = self._new_life(
                IndexLife(model, index.name), ctx
            )
            self._refs_mentioned(ctx, model, index)
        for constraint in snap.options.get("constraints", ()):
            clife = ConstraintLife(model, constraint.name)
            self._new_life(clife, ctx)
            model.constraints[constraint.name] = clife
            self._refs_mentioned(ctx, model, constraint)
        for option in TOGETHER_OPTIONS:
            self._resync_together(model, option, snap.options.get(option), ctx)
        for f in list(self.by_target.get(key, ())):
            if f.creation is not None and f.parent is not model:
                self._ref(f.creation, model)

    def _resync_fields(
        self, model: ModelLife, b: _ModelSnap, a: _ModelSnap, ctx: Element
    ) -> None:
        reason = f"changed by {ctx.rec.describe()}"
        for name in b.fields.keys() - a.fields.keys():
            life = model.fields.pop(name, None)
            if life is not None:
                life.poison(reason)
                self._end_field(life, ctx.rec)
        for name in a.fields.keys() - b.fields.keys():
            life = FieldLife(model, name)
            self._new_life(life, ctx)
            model.fields[name] = life
            self._field_def(ctx, model, life, a.fields[name][0])
        for name in a.fields.keys() & b.fields.keys():
            if (
                a.fields[name][0] is b.fields[name][0]
                and a.fields[name][1] == b.fields[name][1]
            ):
                continue
            existing = model.fields.get(name)
            if existing is None:
                existing = FieldLife(model, name)
                self._new_life(existing, ctx)
                model.fields[name] = existing
            existing.poison(reason)
            self._field_def(ctx, model, existing, a.fields[name][0])

    def _resync_named(
        self,
        model: ModelLife,
        lives: dict[str, Life],
        b: _ModelSnap,
        a: _ModelSnap,
        option: str,
        cls: type[Life],
        ctx: Element,
    ) -> None:
        if _same(b.options.get(option), a.options.get(option)):
            return
        reason = f"changed by {ctx.rec.describe()}"
        before = {o.name: o for o in b.options.get(option, ())}
        after = {o.name: o for o in a.options.get(option, ())}
        for name in before.keys() - after.keys():
            life = lives.pop(name, None)
            if life is not None:
                life.poison(reason)
                life.end = ctx.rec
        for name, obj in after.items():
            if name in before and before[name] is obj:
                continue
            life = lives.get(name)
            if life is None:
                life = cls(model, name)
                self._new_life(life, ctx)
                lives[name] = life
            life.poison(reason)
            self._refs_mentioned(ctx, model, obj)

    def _resync_together(
        self, model: ModelLife, option: str, value: Any, ctx: Element
    ) -> None:
        reason = f"changed by {ctx.rec.describe()}"
        current = model.together[option]
        new: dict[tuple[FieldLife, ...], TogetherLife] = {}
        for entry in normalize_together(value):
            flives = self._field_lives(model, tuple(entry))
            if flives is None:
                continue
            tlife = current.get(flives)
            if tlife is None:
                tlife = TogetherLife(model, option, flives)
                self._new_life(tlife, ctx)
            tlife.poison(reason)
            for f in flives:
                self._ref(ctx, f)
            new[flives] = tlife
        for flives, tlife in current.items():
            if flives not in new:
                tlife.poison(reason)
                tlife.end = ctx.rec
        model.together[option] = new
        model.together_events[option].append(
            TogetherEvent(ctx.rec, "alter", list(new.values()), None)
        )

    # Queries

    def candidates(self) -> Iterable[Life]:
        return (life for life in self.lives if life.is_candidate)
