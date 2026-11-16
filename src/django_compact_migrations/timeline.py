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

from django.conf import settings
from django.db import models
from django.db.migrations import operations as m
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ModelState, ProjectState
from django.db.migrations.utils import resolve_relation
from django.db.models.expressions import F
from django.db.models.query_utils import Q

from django_compact_migrations import backends
from django_compact_migrations.history import History, HistoryError, Key, OpRecord
from django_compact_migrations.locate import (
    TOGETHER_OPTIONS,
    Entry,
    check_sub_elements,
    normalize_together,
)
from django_compact_migrations.opaque import OpaqueOp, Pos
from django_compact_migrations.ops import INERT, OPAQUE, classify, isolated
from django_compact_migrations.verify import DanglingTracker

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
        self.soft_refs: list[SoftRef] = []
        """Periods during which a model option (ordering...) names this life."""

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
        """
        Never removable, nor the subject of a superseded drop. A poisoned
        child also blocks removing its model (the solver checks the whole
        subtree), but doesn't otherwise affect the model or its siblings.
        """
        if reason not in self.poison_reasons:
            self.poison_reasons.append(reason)

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
        self.option_names: set[str] = set()
        """Field names the current ordering / get_latest_by refer to."""
        self.option_element: Element | None = None
        self.active_soft: list[SoftRef] = []
        self.incoming: set[FieldLife] = set()
        """Relation fields (of any model) that ever pointed at this model."""
        self.child_models: set[ModelLife] = set()
        """Models that ever listed this one in their bases."""
        self.managed: set[bool] = set()
        """The values its ``managed`` option took."""
        self.no_ddl: str | None = None
        """Why Django skips this model's schema changes (proxy, swapped)."""

    def note_managed(self, options: Any) -> None:
        # AlterModelOptions sets every alterable option: no "managed" is True.
        self.managed.add(bool(options.get("managed", True)))

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
class SoftRef:
    """
    ``element`` (setting ordering / get_latest_by) names a field from
    ``start`` until ``end`` (None: until the field ends). Removing the field
    alone leaves a stale default ordering, which breaks queries on the model.
    """

    element: Element
    start: Pos
    end: Pos | None = None


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


def option_field_names(options: Any) -> set[str]:
    values: list[Any] = []
    for key in ("ordering", "get_latest_by"):
        value = options.get(key)
        if isinstance(value, (list, tuple)):
            values.extend(value)
        elif value is not None:
            values.append(value)
    names: set[str] = set()
    for value in values:
        if isinstance(value, str):
            # "-field", "?", "fk__name" (a lookup through the local fk).
            names.add(value.lstrip("-?+").split("__")[0])
        else:
            # F(), OrderBy and other expressions: every identifier in them.
            names |= mentioned_names(value)
    return names


def _is_expression(value: Any) -> bool:
    return hasattr(value, "resolve_expression") and not isinstance(value, type)


def _resolve(model: Any, app_label: str, model_name: str) -> Key:
    # Migrations name related models by string, or by a model class (_meta).
    app, name = resolve_relation(model, app_label, model_name)
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


# Options whose changes are tracked on child lives rather than the model.
_CHILD_OPTIONS = frozenset({"indexes", "constraints", *TOGETHER_OPTIONS})


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


class InconsistentHistory(Exception):
    """An operation refers to something that doesn't exist at that point."""


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
        self._lock_skipped_ddl()

    def _lock_skipped_ddl(self) -> None:
        """
        Django skips the schema changes of unmanaged, proxy and swapped-out
        models: a table or column it left behind, or never created, must
        stay so. Removing or folding operations of a model that changes
        between managed and unmanaged would move that line, so nothing of
        it is touched; nor of proxy or swapped-out models.
        """
        for life in self.lives:
            if not isinstance(life, ModelLife):
                continue
            reason = life.no_ddl
            if reason is None and len(life.managed) > 1:
                reason = "switched between managed and unmanaged"
            if reason is None:
                continue
            for sub in life.subtree():
                sub.poison(
                    f"its model is {reason}" if sub is not life else f"it is {reason}"
                )

    def _state_forwards(self, op: Operation, app_label: str, rec: OpRecord) -> None:
        try:
            isolated(op).state_forwards(app_label, self.state)
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
        verb: str = "changed",
    ) -> Element:
        if ctx is not None:
            if owner is not None:
                owner.poison(f"{verb} by {ctx.rec.describe()}")
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
    def _ref(element: Element, life: Life | None) -> None:
        if life is None:
            return
        element.refs.append(life)
        life.referenced_by.append(element)

    def _model(
        self, app_label: str, name: str, rec: OpRecord, ctx: Element | None
    ) -> ModelLife:
        life = self.models.get((app_label, name.lower()))
        if life is None:
            # Django's own state replay would fail on it right after.
            raise InconsistentHistory(
                f"{rec.describe()} refers to the model {app_label}.{name},"
                " which doesn't exist at that point"
            )
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
            targets.add(to_key)
            target = self.models.get(to_key)
            remote_life = target
            if target is not None:
                target.incoming.add(life)
                self._ref(element, target)
                to_fields = getattr(fld, "to_fields", None)
                if not to_fields or None in to_fields:
                    self._ref(element, self._pk(target))
                for name in to_fields or ():
                    if name is not None:
                        self._ref(element, target.fields.get(name))
            through = getattr(remote, "through", None)
            if through is not None:
                through_key = _resolve(through, app_label, model_name)
                targets.add(through_key)
                through_life = self.models.get(through_key)
                if through_life is not None:
                    self._ref(element, through_life)
                    for name in getattr(remote, "through_fields", None) or ():
                        self._ref(element, through_life.fields.get(name))
        local: set[str] = set()
        for attr in ("from_fields", "field_names"):
            local.update(
                n for n in getattr(fld, attr, None) or () if isinstance(n, str)
            )
        # Rendering the state clones fields through deconstruct(): it works.
        for value in fld.deconstruct()[3].values():
            if _is_expression(value):
                local |= mentioned_names(value)
        for name in local:
            if name != "self":
                self._ref_local(element, model, name)
        self._set_targets(life, targets)
        life.defs.append((element, fld, remote_life))

    def _ref_local(self, element: Element, model: ModelLife, name: str) -> None:
        target = model.fields.get(name)
        if target is not None:
            self._ref(element, target)

    def _refs_mentioned(self, element: Element, model: ModelLife, obj: Any) -> None:
        for name in mentioned_names(obj):
            self._ref_local(element, model, name)

    def _set_ordering(
        self, element: Element, model: ModelLife, options: Any, rec: OpRecord
    ) -> None:
        """
        Record which fields the model's ordering / get_latest_by now name.
        Fields added (or renamed into a named name) later are matched too:
        an option may name a field before it exists.
        """
        pos = (rec.migration.index, rec.index)
        for soft in model.active_soft:
            soft.end = pos
        model.active_soft = []
        model.option_names = option_field_names(options)
        model.option_element = element
        for name in model.option_names:
            life = model.fields.get(name)
            if life is not None:
                self._start_soft(model, life, rec)

    def _start_soft(self, model: ModelLife, life: FieldLife, rec: OpRecord) -> None:
        assert model.option_element is not None
        soft = SoftRef(model.option_element, (rec.migration.index, rec.index))
        life.soft_refs.append(soft)
        model.active_soft.append(soft)

    def _field_named(self, model: ModelLife, life: FieldLife, rec: OpRecord) -> None:
        """A field got a name (added or renamed): the options may name it."""
        if life.names[-1] in model.option_names and model.option_element is not None:
            self._start_soft(model, life, rec)

    def _field_unnamed(self, model: ModelLife, life: FieldLife, rec: OpRecord) -> None:
        """A field lost its name (removed or renamed)."""
        pos = (rec.migration.index, rec.index)
        for soft in list(model.active_soft):
            if soft in life.soft_refs:
                soft.end = pos
                model.active_soft.remove(soft)

    def _end_field(self, life: FieldLife, rec: OpRecord) -> None:
        self._field_unnamed(life.model, life, rec)
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
        model.note_managed(op.options)
        if op.options.get("proxy"):
            model.no_ddl = "a proxy model, without a table of its own"
        swappable = op.options.get("swappable")
        if swappable:
            setting = getattr(settings, swappable, None)
            if isinstance(setting, str) and setting.lower() != f"{key[0]}.{key[1]}":
                model.no_ddl = f"swapped out for {setting} ({swappable})"
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
        self._set_ordering(element, model, op.options, rec)
        for base in op.bases or ():
            if isinstance(base, str) or hasattr(base, "_meta"):
                base_life = self.models.get(_resolve(base, app_label, op.name_lower))
                if base_life is not None:
                    self._ref(element, base_life)
                    base_life.child_models.add(model)
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
        self._element(rec, None, model, ctx, "ended")
        model.end = rec
        del self.models[model.key]
        for f in model.fields.values():
            self._set_targets(f, set())

    def _on_RenameModel(
        self, rec: OpRecord, op: m.RenameModel, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.old_name, rec, ctx)
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
    ) -> tuple[ModelLife, Element]:
        model = self._model(app_label, op.name, rec, ctx)
        return model, self._element(rec, None, model, ctx)

    def _on_AlterModelTable(
        self, rec: OpRecord, op: m.AlterModelTable, app_label: str, ctx: Element | None
    ) -> None:
        model, _ = self._model_op(rec, op, app_label, ctx)
        if isinstance(op.table, str):
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
        model.note_options(op.options)
        model.note_managed(op.options)
        self._set_ordering(element, model, op.options, rec)

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
        if op.order_with_respect_to:
            self._ref_local(element, model, op.order_with_respect_to)

    def _on_AlterUniqueTogether(
        self,
        rec: OpRecord,
        op: m.AlterUniqueTogether | m.AlterIndexTogether,
        app_label: str,
        ctx: Element | None,
    ) -> None:
        # Inside an opaque op only the together entries are touched (and
        # poisoned below), not the model's other options.
        model = self._model(app_label, op.name, rec, ctx)
        element = self._element(rec, None, model if ctx is None else None, ctx)
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
        # AlterUniqueTogether/AlterIndexTogether hold a set: no duplicates.
        for entry, flives in resolved:
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

    def _field(self, model: ModelLife, name: str, rec: OpRecord) -> FieldLife:
        life = model.fields.get(name)
        if life is None:
            # Django's state replay fails on RemoveField and RenameField of
            # a missing field, and migrating an AlterField of one fails.
            raise InconsistentHistory(
                f"{rec.describe()} refers to the field {model.label}.{name},"
                " which doesn't exist at that point"
            )
        return life

    def _on_AddField(
        self, rec: OpRecord, op: m.AddField, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
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
        self._field_named(model, life, rec)

    def _on_RemoveField(
        self, rec: OpRecord, op: m.RemoveField, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        life = self._field(model, op.name, rec)
        self._element(rec, None, life, ctx, "ended")
        del model.fields[op.name]
        self._end_field(life, rec)

    def _on_AlterField(
        self, rec: OpRecord, op: m.AlterField, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        life = self._field(model, op.name, rec)
        element = self._element(rec, None, life, ctx)
        self._field_def(element, model, life, op.field)

    def _on_RenameField(
        self, rec: OpRecord, op: m.RenameField, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
        life = self._field(model, op.old_name, rec)
        element = self._element(rec, None, life, ctx)
        if element is not ctx:
            element.info["rename"] = True
        self._field_unnamed(model, life, rec)
        del model.fields[op.old_name]
        if op.new_name in model.fields:
            model.fields[op.new_name].poison(f"name taken over in {rec.describe()}")
            life.poison(f"name taken over in {rec.describe()}")
        model.fields[op.new_name] = life
        life.names.append(op.new_name)
        self._field_named(model, life, rec)

    def _on_AddIndex(
        self, rec: OpRecord, op: m.AddIndex, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
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
        life = model.indexes.pop(op.name, None)
        if life is None:
            model.poison(f"unknown index {op.name!r} in {rec.describe()}")
            self._element(rec, None, model, ctx)
            return
        self._element(rec, None, life, ctx, "ended")
        life.end = rec

    def _on_RenameIndex(
        self, rec: OpRecord, op: m.RenameIndex, app_label: str, ctx: Element | None
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
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
        life = model.constraints.pop(op.name, None)
        if life is None:
            model.poison(f"unknown constraint {op.name!r} in {rec.describe()}")
            self._element(rec, None, model, ctx)
            return
        self._element(rec, None, life, ctx, "ended")
        life.end = rec

    def _on_AlterConstraint(
        self,
        rec: OpRecord,
        op: Any,
        app_label: str,
        ctx: Element | None,
    ) -> None:
        model = self._model(app_label, op.model_name, rec, ctx)
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
            # The lives always mirror the models of the state.
            model = self.models.pop(key)
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
            model = self.models[key]
            if (
                a.name != b.name
                or not _same(b.bases, a.bases)
                or not _same(b.managers, a.managers)
                or any(
                    not _same(b.options.get(k), a.options.get(k))
                    for k in b.options.keys() | a.options.keys()
                    if k not in _CHILD_OPTIONS
                )
            ):
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
            if any(
                not _same(b.options.get(k), a.options.get(k))
                for k in ("ordering", "get_latest_by")
            ):
                self._set_ordering(ctx, model, a.options, ctx.rec)
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
        self._set_ordering(ctx, model, snap.options, ctx.rec)
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
            life = model.fields.pop(name)
            life.poison(reason)
            self._end_field(life, ctx.rec)
        for name in a.fields.keys() - b.fields.keys():
            life = FieldLife(model, name)
            self._new_life(life, ctx)
            model.fields[name] = life
            self._field_def(ctx, model, life, a.fields[name][0])
            self._field_named(model, life, ctx.rec)
        for name in a.fields.keys() & b.fields.keys():
            if (
                a.fields[name][0] is b.fields[name][0]
                and a.fields[name][1] == b.fields[name][1]
            ):
                continue
            existing = model.fields[name]
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
            gone = lives.pop(name)
            gone.poison(reason)
            gone.end = ctx.rec
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


def first_field(life: Life) -> FieldLife | None:
    """
    The field of the first column of an index, unique constraint or
    together entry: on MySQL it decides which index backs a foreign key.
    """
    if isinstance(life, TogetherLife):
        return life.fields[0] if life.fields else None
    if not isinstance(life, (IndexLife, ConstraintLife)):
        return None
    creation = life.creation
    assert creation is not None
    op, sub = creation.rec.op, creation.sub
    if sub is None:
        obj = getattr(op, "index", None) or getattr(op, "constraint", None)
    else:
        key = "indexes" if sub[0] == "index" else "constraints"
        obj = op.options[key][sub[1]]  # type: ignore[attr-defined]
    name = backends.first_field_name(obj)
    return next(
        (r for r in creation.refs if isinstance(r, FieldLife) and name in r.names),
        None,
    )


def ever_foreign_key(life: FieldLife) -> bool:
    return any(backends.is_foreign_key(d) for _, d, _ in life.defs)


def backs_foreign_key(life: Life) -> FieldLife | None:
    """
    The foreign key, alive at the end of the history, whose backing index
    on MySQL depends on whether ``life`` (an index, unique constraint or
    together entry starting with it) ever existed.
    """
    field = first_field(life)
    return field if field is not None and lasting_foreign_key(field) else None


def lasting_foreign_key(field: FieldLife) -> bool:
    """Ever a foreign key, and its column not dropped later anyway."""
    return ever_foreign_key(field) and field.end is None and field.model.end is None
