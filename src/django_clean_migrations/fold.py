"""
Folding: move a later operation into the operation that created the thing it
changes, when nothing in between can tell the difference.

``P`` is the earlier element that creates (and keeps carrying) a definition,
``E`` the later operation folded into it and deleted. P's operation is then
replaced by a regenerated one. Each round makes at most one change per
operation as ``E`` and never touches an operation that also loses parts in
the same round; the core runs rounds until nothing changes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.db.migrations import operations as m
from django.db.migrations.operations.base import Operation
from django.db.models import NOT_PROVIDED

from django_clean_migrations.locate import normalize_together
from django_clean_migrations.ops import classify
from django_clean_migrations.timeline import (
    ConstraintLife,
    Element,
    FieldLife,
    IndexLife,
    Life,
    ModelLife,
)

if TYPE_CHECKING:
    from django_clean_migrations.solver import Solver


def _occupied(fld: Any) -> tuple[Any, ...]:
    from django_clean_migrations.solver import _occupied as occupied

    return occupied(fld)


FOLD_KINDS = (
    "field",
    "add_field",
    "index",
    "constraint",
    "together",
    "options",
    "managers",
    "comment",
)


@dataclass(eq=False)
class Fold:
    kind: str
    """One of FOLD_KINDS."""
    target: Element
    """P: the creating element that absorbs E."""
    folded: Element
    """E: the operation deleted."""
    life: Life
    """The field, index, constraint or model concerned."""


RegenerationCheck = Callable[[Element, Operation], "str | None"]


class Folder:
    def __init__(self, solver: Solver, can_regenerate: RegenerationCheck) -> None:
        self.solver = solver
        self.history = solver.history
        self.can_regenerate = can_regenerate
        self.pending: dict[int, Operation] = {}
        """rec.seq of P -> the regenerated operation built so far."""
        self.used: set[int] = set()
        """rec.seq of operations deleted as E this round."""
        self.into_create: dict[int, set[FieldLife]] = {}
        """rec.seq of a CreateModel -> field lives folded into it this round."""
        self.folds: list[Fold] = []

    # Shared conditions

    def _touched(self, seq: int) -> bool:
        """Removed, or losing parts, in this round's other passes."""
        return any(self.solver.removed(e) for e in self.solver.timeline.elements[seq])

    def _in_scope(self, life: Life) -> bool:
        scope = self.solver.scope
        return scope.selected(life) and not scope.excluded(life)

    def _usable_target(self, p: Element) -> bool:
        return (
            p.rec.not_editable_reason() is None
            and not self._touched(p.rec.seq)
            and p.rec.seq not in self.used
        )

    def _usable_folded(self, e: Element) -> bool:
        return (
            e.sub is None
            and not e.opaque
            and e.not_editable_reason() is None
            and not self._touched(e.rec.seq)
            and e.rec.seq not in self.used
            and e.rec.seq not in self.pending
            and self.solver.unit_key(e) not in self.solver.trial_blocked
        )

    def _before(self, life: Life, p: Element) -> bool:
        """Whether ``life`` certainly exists when ``p`` runs."""
        creation = life.creation
        if creation is None:
            return False
        if creation.rec is p.rec:
            return True
        c, at = creation.pos, p.pos
        if c[0] == at[0]:
            started = c[1] < at[1]
        else:
            started = bool(self.history.ancestors[at[0]] >> c[0] & 1)
        if not started:
            return False
        end = life.end_pos
        if end is None:
            return True
        if end[0] == at[0]:
            return end[1] > at[1]
        return bool(self.history.descendants[at[0]] >> end[0] & 1)

    def _in_create(self, life: Life, create: Element) -> bool:
        if life.creation is not None and life.creation.rec is create.rec:
            return True
        return isinstance(life, FieldLife) and life in self.into_create.get(
            create.rec.seq, set()
        )

    def _requirements(self, refs: Iterable[Life], p: Element, model: ModelLife) -> bool:
        for life in refs:
            if life is model:
                continue
            if (
                isinstance(life, FieldLife)
                and life.parent is model
                and p.rec.name == "CreateModel"
            ):
                if not self._in_create(life, p):
                    return False
                continue
            if not self._before(life, p):
                return False
        return True

    def _stable_names(self, lives: Iterable[Life], p: Element, e: Element) -> bool:
        """
        The folded definition names fields and models as they are at E; they
        must have had the same names at P.
        """
        for life in lives:
            for el in life.elements:
                renames = el.info.get("rename") or type(el.rec.op) is m.RenameModel
                if (
                    renames
                    and not self.solver.removed(el)
                    and self.solver._between(el, p.pos, e.pos)
                ):
                    return False
        return True

    def _quiet(self, names: set[str], p: Element, e: Element, model: ModelLife) -> bool:
        return (
            self.solver._opaque_block(names, p.pos, e.pos, None, model.start_pos, e)
            is None
        )

    def _no_refs_between(self, life: Life, p: Element, e: Element) -> bool:
        return not any(
            not self.solver.removed(r) and self.solver._between(r, p.pos, e.pos)
            for r in life.referenced_by
            if r is not e and r is not p
        )

    def _commit(
        self, kind: str, p: Element, e: Element, life: Life, op: Operation
    ) -> bool:
        reason = self.can_regenerate(p, op)
        if reason is not None:
            return False
        self.pending[p.rec.seq] = op
        self.used.add(e.rec.seq)
        self.solver.plan.dropped[e] = "fold"
        self.folds.append(Fold(kind, p, e, life))
        return True

    def _current(self, p: Element) -> Operation:
        return self.pending.get(p.rec.seq, p.rec.op)

    # Entry point

    def run(self) -> list[Fold]:
        kinds = self.solver.fold_kinds
        lives = self.solver.timeline.lives
        removed = self.solver.plan.removed
        for life in lives:
            if (
                not isinstance(life, ModelLife)
                or life in removed
                or life.poisoned
                or not self._in_scope(life)
            ):
                continue
            create = life.creation
            if (
                create is None
                or create.opaque
                or type(create.rec.op) is not m.CreateModel
                or not self._usable_target(create)
            ):
                continue
            if self._ordered_wrt(life):
                continue
            if "add_field" in kinds:
                self._fold_add_fields(life, create)
            if "index" in kinds:
                self._fold_named(life, create, IndexLife, "indexes", "index")
            if "constraint" in kinds:
                self._fold_named(
                    life, create, ConstraintLife, "constraints", "constraint"
                )
            if "together" in kinds:
                self._fold_together(life, create)
            for kind, cls in (
                ("options", m.AlterModelOptions),
                ("managers", m.AlterModelManagers),
                ("comment", m.AlterModelTableComment),
            ):
                if kind in kinds:
                    self._fold_model_op(life, create, kind, cls)
        if "field" in kinds:
            for life in lives:
                if isinstance(life, FieldLife):
                    self._fold_alter_field(life)
        return self.folds

    def _ordered_wrt(self, model: ModelLife) -> bool:
        # order_with_respect_to adds an implicit _order column, last.
        create = model.creation
        assert create is not None
        if create.rec.op.options.get("order_with_respect_to"):  # type: ignore[attr-defined]
            return True
        return any(type(e.rec.op) is m.AlterOrderWithRespectTo for e in model.elements)

    # 1. AlterField -> its carrier

    def _fold_alter_field(self, life: FieldLife) -> None:
        solver = self.solver
        model = life.model
        if (
            life.poisoned
            or life.ever_pk
            or life in solver.plan.removed
            or model in solver.plan.removed
            or model.poisoned
            or not life.defs
            or not self._in_scope(life)
        ):
            return
        p = life.creation
        if p is None or p.opaque:
            return
        if type(p.rec.op) not in (m.AddField, m.CreateModel):
            return
        own = sorted(
            (e for e in life.elements if not solver.removed(e) and e is not p),
            key=lambda e: e.rec.seq,
        )
        if not own or type(own[0].rec.op) is not m.AlterField:
            return
        e = own[0]
        if not (self._usable_target(p) and self._usable_folded(e)):
            return
        if not self.history.ordered(p.pos[0], e.pos[0]):
            return
        defs = {id(el): (fld, target) for el, fld, target in life.defs}
        if id(p) not in defs or id(e) not in defs:
            return
        dp, tp = defs[id(p)]
        de, _ = defs[id(e)]
        if getattr(dp, "many_to_many", False) or getattr(de, "many_to_many", False):
            return
        if _occupied(dp) != _occupied(de):
            return
        released = set(p.refs) - set(e.refs)
        if any(
            not (r is tp or (isinstance(r, FieldLife) and r.parent is tp and r.ever_pk))
            for r in released
        ):
            return
        if not self._requirements(set(e.refs) - set(p.refs), p, model):
            return
        if not self._stable_names([*e.refs, model], p, e):
            return
        if not self._no_refs_between(life, p, e):
            return
        if not self._quiet(life.scan_names() | model.scan_names(), p, e, model):
            return
        new = self._alter_into(p, e)
        if new is not None:
            self._commit("field", p, e, life, new)

    def _alter_into(self, p: Element, e: Element) -> Operation | None:
        alter = e.rec.op
        assert isinstance(alter, m.AlterField)
        current = self._current(p)
        if type(current) is m.AddField:
            field = alter.field
            preserve = alter.preserve_default
            if (
                field.default is NOT_PROVIDED
                and current.field.default is not NOT_PROVIDED
            ):
                # The table may hold rows when the column is added: keep the
                # earlier default for them, as a one-off.
                field = field.clone()
                field.default = current.field.default
                preserve = False
            return m.AddField(
                model_name=current.model_name,
                name=current.name,
                field=field,
                preserve_default=preserve,
            )
        if type(current) is m.CreateModel:
            assert p.sub is not None
            name = p.sub[1]
            fields = [
                (
                    n,
                    _state_field(alter.field, alter.preserve_default)
                    if n == name
                    else f,
                )
                for n, f in current.fields
            ]
            return _create(current, fields=fields)
        return None

    # 2. AddField -> CreateModel

    def _fold_add_fields(self, model: ModelLife, create: Element) -> None:
        solver = self.solver
        fields = sorted(
            (
                c
                for c in model.children
                if isinstance(c, FieldLife)
                and c not in solver.plan.removed
                and c.creation is not None
                and c.creation.rec is not create.rec
            ),
            key=lambda c: c.creation.rec.seq,  # type: ignore[union-attr]
        )
        for life in fields:
            # Fields must stay in their order: stop at the first that can't go.
            if not self._fold_add_field(model, create, life):
                return

    def _fold_add_field(
        self, model: ModelLife, create: Element, life: FieldLife
    ) -> bool:
        a = life.creation
        if (
            a is None
            or a.opaque
            or type(a.rec.op) is not m.AddField
            or life.poisoned
            or life.ever_pk
            or not self._in_scope(life)
            or not self._usable_folded(a)
            or not self.history.ordered(create.pos[0], a.pos[0])
        ):
            return False
        names = {n.lower() for n in life.names} | life.extra_names
        for sibling in model.children:
            if (
                sibling is not life
                and isinstance(sibling, FieldLife)
                and sibling not in self.solver.plan.removed
                and names & ({n.lower() for n in sibling.names} | sibling.extra_names)
            ):
                return False
        if not self._requirements(a.refs, create, model):
            return False
        if not self._stable_names([*a.refs, model], create, a):
            return False
        if not self._quiet(model.scan_names(), create, a, model):
            return False
        add = a.rec.op
        assert isinstance(add, m.AddField)
        current = self._current(create)
        assert isinstance(current, m.CreateModel)
        new = _create(
            current,
            fields=[
                *current.fields,
                (add.name, _state_field(add.field, add.preserve_default)),
            ],
        )
        if not self._commit("add_field", create, a, life, new):
            return False
        self.into_create.setdefault(create.rec.seq, set()).add(life)
        return True

    # 3. AddIndex / AddConstraint -> CreateModel options

    def _fold_named(
        self,
        model: ModelLife,
        create: Element,
        cls: type[Life],
        option: str,
        kind: str,
    ) -> None:
        lives = sorted(
            (
                c
                for c in model.children
                if type(c) is cls
                and c not in self.solver.plan.removed
                and c.creation is not None
                and c.creation.rec is not create.rec
            ),
            key=lambda c: c.creation.rec.seq,  # type: ignore[union-attr]
        )
        for life in lives:
            if not self._fold_one_named(model, create, life, option, kind):
                return

    def _fold_one_named(
        self, model: ModelLife, create: Element, life: Life, option: str, kind: str
    ) -> bool:
        x = life.creation
        expected = "AddIndex" if kind == "index" else "AddConstraint"
        if (
            x is None
            or x.opaque
            or classify(x.rec.op) != expected
            or life.poisoned
            or not self._in_scope(life)
            or not self._usable_folded(x)
            or not self.history.ordered(create.pos[0], x.pos[0])
        ):
            return False
        names = {n.lower() for n in life.names}
        for other in self.solver.timeline.lives:
            if (
                other is not life
                and isinstance(other, (IndexLife, ConstraintLife))
                and other not in self.solver.plan.removed
                and names & {n.lower() for n in other.names}
            ):
                return False
        if not self._requirements(x.refs, create, model):
            return False
        if not self._stable_names([*x.refs, model], create, x):
            return False
        obj = getattr(x.rec.op, "index" if kind == "index" else "constraint")
        scan = set(names)
        if kind == "constraint" or getattr(obj, "condition", None) is not None:
            scan |= model.scan_names()
        if not self._quiet(scan, create, x, model):
            return False
        current = self._current(create)
        assert isinstance(current, m.CreateModel)
        options = dict(current.options)
        options[option] = [*options.get(option, []), obj]
        return self._commit(kind, create, x, life, _create(current, options=options))

    # 4. Together options

    def _fold_together(self, model: ModelLife, create: Element) -> None:
        for option, events in model.together_events.items():
            alters = [ev for ev in events if ev.kind != "create"]
            if not alters or alters[0].kind != "alter" or alters[0].element is None:
                continue
            e = alters[0].element
            if not self._usable_folded(e):
                continue
            if not self.history.ordered(create.pos[0], e.pos[0]):
                continue
            if any(
                not all(self._in_create(f, create) for f in t.fields)
                for t in alters[0].lives
            ):
                continue
            if not self._stable_names(
                [model, *(f for t in alters[0].lives for f in t.fields)], create, e
            ):
                continue
            if option == "unique_together" and not self._quiet(
                model.scan_names(), create, e, model
            ):
                continue
            op = e.rec.op
            assert isinstance(op, (m.AlterUniqueTogether, m.AlterIndexTogether))
            current = self._current(create)
            assert isinstance(current, m.CreateModel)
            options = dict(current.options)
            value = normalize_together(op.option_value)
            if value:
                options[option] = set(value)
            else:
                options.pop(option, None)
            self._commit(
                "together", create, e, model, _create(current, options=options)
            )

    # 5. Model options, managers, table comment

    def _fold_model_op(
        self, model: ModelLife, create: Element, kind: str, cls: type[Operation]
    ) -> None:
        solver = self.solver
        own = sorted(
            (
                e
                for e in model.elements
                if e.sub is None
                and not e.opaque
                and type(e.rec.op) is cls
                and not solver.removed(e)
            ),
            key=lambda e: e.rec.seq,
        )
        if not own:
            return
        e = own[0]
        if not self._usable_folded(e) or not self.history.ordered(
            create.pos[0], e.pos[0]
        ):
            return
        current = self._current(create)
        assert isinstance(current, m.CreateModel)
        op = e.rec.op
        if isinstance(op, m.AlterModelOptions):
            keys = m.AlterModelOptions.ALTER_OPTION_KEYS
            if bool(op.options.get("managed", True)) != bool(
                current.options.get("managed", True)
            ):
                return
            options = {**current.options, **op.options}
            for key in keys:
                if key not in op.options:
                    options.pop(key, None)
            from django_clean_migrations.timeline import _option_field_names

            for name in _option_field_names(op.options):
                life = model.fields.get(name) or next(
                    (
                        f
                        for f in model.children
                        if isinstance(f, FieldLife) and name in f.names
                    ),
                    None,
                )
                if life is not None and not (
                    self._in_create(life, create)
                    and self._stable_names([life], create, e)
                ):
                    return
            new = _create(current, options=options)
        elif isinstance(op, m.AlterModelManagers):
            new = _create(current, managers=list(op.managers))
        else:
            assert isinstance(op, m.AlterModelTableComment)
            new = _create(
                current,
                options={**current.options, "db_table_comment": op.table_comment},
            )
        names = solver._query_names(model)
        if not self._quiet(names, create, e, model):
            return
        self._commit(kind, create, e, model, new)


def _state_field(field: Any, preserve_default: bool) -> Any:
    """The definition the project state holds after Add/AlterField."""
    if preserve_default:
        return field
    field = field.clone()
    field.default = NOT_PROVIDED
    return field


def _create(
    op: m.CreateModel,
    *,
    fields: list[tuple[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
    managers: list[Any] | None = None,
) -> m.CreateModel:
    return m.CreateModel(
        name=op.name,
        fields=list(op.fields) if fields is None else fields,
        options=dict(op.options) if options is None else options,
        bases=op.bases,
        managers=op.managers if managers is None else managers,
    )
