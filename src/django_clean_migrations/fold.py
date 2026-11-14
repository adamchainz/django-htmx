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

import re
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
    "rename",
    "table",
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
            and "fold:" + self.solver.unit_key(e) not in self.solver.trial_blocked
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
                # Database object names embed table and column names as they
                # were at creation: AlterModelTable counts as a rename.
                renames = el.info.get("rename") or type(el.rec.op) in (
                    m.RenameModel,
                    m.AlterModelTable,
                )
                if (
                    renames
                    and not self.solver.removed(el)
                    and self.solver._between(el, p.pos, e.pos)
                ):
                    return False
        return True

    def _targets_unaltered(self, lives: Iterable[Life], p: Element, e: Element) -> bool:
        """
        No AlterField of a field the folded definition points at (a target
        pk or to_field) may run in the window: the schema editor drops and
        recreates foreign keys pointing at a field whose type changes, and
        recreates them under a different name ("..._fk" instead of
        "..._fk_<table>_<column>"). A foreign key that now exists earlier
        would go through that and come out named differently.
        """
        for life in lives:
            if not isinstance(life, FieldLife):
                continue
            for el in life.elements:
                if (
                    type(el.rec.op) is m.AlterField
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

    def _no_setup_between(self, field: Any, p: Element, e: Element) -> bool:
        """
        A field class from outside django.db.models may need something set
        up in the database (an extension such as hstore, a type, a function),
        possibly by a RunSQL or custom operation between P and E: creating
        the column at P would come before it. Such fields aren't folded
        across any opaque operation other than RunPython. Nor are fields
        with a ``db_collation``, which such an operation may create
        (NetBox's natural_sort collation).
        """
        if type(field).__module__.startswith("django.db.models") and not getattr(
            field, "db_collation", None
        ):
            return True
        index = self.solver.opaque
        return not any(
            type(o.rec.op) is not m.RunPython
            and index.contains(o, p.pos, e.pos, p.pos)
            and self.solver._between(
                self.solver.opaque_elements[o.rec.seq], p.pos, e.pos
            )
            for o in index.ops
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
            if "table" in kinds:
                self._fold_table(life, create)
        if "field" in kinds:
            for life in lives:
                if isinstance(life, FieldLife):
                    self._fold_alter_field(life)
        if "rename" in kinds:
            for life in lives:
                if isinstance(life, FieldLife):
                    self._fold_rename_field(life)
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
        if _occupied(dp) != _occupied(de) or not same_named_objects(dp, de):
            return
        # An AlterField changing the type of a field that foreign keys point
        # at (via to_field) recreates those keys under other names.
        if referenced_by_fields(life):
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
        if not self._targets_unaltered(set(e.refs) - set(p.refs), p, e):
            return
        if not self._no_refs_between(life, p, e):
            return
        if not self._no_setup_between(de, p, e):
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
        add = a.rec.op
        assert isinstance(add, m.AddField)
        names = {n.lower() for n in life.names} | life.extra_names
        for sibling in model.children:
            if (
                sibling is not life
                and isinstance(sibling, FieldLife)
                and sibling not in self.solver.plan.removed
                and names & ({n.lower() for n in sibling.names} | sibling.extra_names)
            ):
                return False
        # A RenameField of another field in between moved that field to the
        # end of the state's fields, ahead of this one: folded, this one
        # would come first (and SQLite rebuilds tables in that order), which
        # matters if that field stays to the end.
        for sibling in model.children:
            if (
                sibling is life
                or not isinstance(sibling, FieldLife)
                or sibling.end is not None  # dropped later: its place won't matter
            ):
                continue
            for el in sibling.elements:
                if (
                    el.info.get("rename")
                    and not self.solver.removed(el)
                    and self.solver._between(el, create.pos, a.pos)
                ):
                    return False
        if not self._requirements(a.refs, create, model):
            return False
        if not self._stable_names([*a.refs, model], create, a):
            return False
        own_pk = [f for f in model.children if isinstance(f, FieldLife) and f.ever_pk]
        many_to_many = getattr(add.field, "many_to_many", False)
        # The foreign keys of an auto-created through table point at both
        # models' primary keys.
        if not self._targets_unaltered(
            [*a.refs, *(own_pk if many_to_many else [])], create, a
        ):
            return False
        if not self._no_setup_between(add.field, create, a):
            return False
        if not self._quiet(model.scan_names(), create, a, model):
            return False
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

    # 6. RenameField -> the field's creation

    def _fold_rename_field(self, life: FieldLife) -> None:
        """
        Give the field its new name where it is created, and delete the
        RenameField. Django's RenameField only renames the column: indexes,
        unique and check constraints, foreign key constraints and many-to-many
        tables keep names derived from the old column or field name, while a
        field created under the new name gets names derived from it. So only
        fields without any such object are folded (or whose column is fixed
        by db_column, so it doesn't change at all).
        """
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
        if p is None or p.opaque or type(p.rec.op) not in (m.AddField, m.CreateModel):
            return
        own = sorted(
            (e for e in life.elements if not solver.removed(e) and e is not p),
            key=lambda e: e.rec.seq,
        )
        if not own or type(own[0].rec.op) is not m.RenameField:
            return
        e = own[0]
        rename = e.rec.op
        assert isinstance(rename, m.RenameField)
        if not (self._usable_target(p) and self._usable_folded(e)):
            return
        if not self.history.ordered(p.pos[0], e.pos[0]):
            return
        defs = {id(el): fld for el, fld, _ in life.defs}
        field = defs.get(id(p))
        if field is None or not _rename_keeps_names(field):
            return
        # Nothing may name the field between P and E (other fields' to_field,
        # indexes, constraints, options), nor any field point at it.
        if referenced_by_fields(life) or not self._no_refs_between(life, p, e):
            return
        current = self._current(p)
        if type(current) is m.CreateModel and _mentions(
            current.options, rename.old_name
        ):
            return
        # The new name must be free from P onward.
        new = rename.new_name.lower()
        for sibling in model.children:
            if (
                sibling is not life
                and isinstance(sibling, FieldLife)
                and sibling not in solver.plan.removed
                and new in {n.lower() for n in sibling.names}
            ):
                return
        if not self._stays_last(life, model, p, e):
            return
        if not self._quiet(life.scan_names() | model.scan_names(), p, e, model):
            return
        op = self._renamed_into(p, current, rename)
        if op is not None:
            self._commit("rename", p, e, life, op)

    def _stays_last(
        self, life: FieldLife, model: ModelLife, p: Element, e: Element
    ) -> bool:
        """
        RenameField moves the field to the end of the model state's fields,
        which decides the column order of SQLite table rebuilds: folded, the
        field keeps its place. So the field must already be last when it is
        renamed: no other field of the model gets created or renamed after
        P (or after it in the same CreateModel) and before E.
        """
        current = self._current(p)
        if type(current) is m.CreateModel:
            assert p.sub is not None
            names = [n for n, _ in current.fields]
            if names and names[-1] != p.sub[1]:
                return False
        for sibling in model.children:
            if sibling is life or not isinstance(sibling, FieldLife):
                continue
            if sibling in self.solver.plan.removed:
                continue
            for el in sibling.elements:
                naming = el is sibling.creation or el.info.get("rename")
                if (
                    naming
                    and el.rec is not p.rec
                    and not self.solver.removed(el)
                    and self.solver._between(el, p.pos, e.pos)
                ):
                    return False
        return True

    def _renamed_into(
        self, p: Element, current: Operation, rename: m.RenameField
    ) -> Operation | None:
        if type(current) is m.AddField:
            return m.AddField(
                model_name=current.model_name,
                name=rename.new_name,
                field=current.field,
                preserve_default=current.preserve_default,
            )
        if type(current) is m.CreateModel:
            assert p.sub is not None
            return _create(
                current,
                fields=[
                    (rename.new_name if n == p.sub[1] else n, f)
                    for n, f in current.fields
                ],
            )
        return None

    # AlterModelTable -> CreateModel options, for unmanaged models only

    def _fold_table(self, model: ModelLife, create: Element) -> None:
        """
        A table created under a name and renamed later keeps the names its
        primary key constraint, sequence or identity, indexes and foreign
        keys got from the old name (Django renames the table only), so a
        managed model's AlterModelTable is never folded. An unmanaged model
        has no table to rename: its AlterModelTable is folded into the
        CreateModel, as long as the model stays unmanaged in between.
        """
        solver = self.solver
        current = self._current(create)
        if not isinstance(current, m.CreateModel):
            return
        if current.options.get("managed", True):
            return
        own = sorted(
            (
                e
                for e in model.elements
                if e.sub is None and not e.opaque and not solver.removed(e)
            ),
            key=lambda e: e.rec.seq,
        )
        tables = [e for e in own if type(e.rec.op) is m.AlterModelTable]
        if not tables:
            return
        e = tables[0]
        if not self._usable_folded(e) or not self.history.ordered(
            create.pos[0], e.pos[0]
        ):
            return
        for other in own:
            # Whatever else happens to the model in between (options that
            # could make it managed, renames) keeps it as it is.
            if other is not e and solver._between(other, create.pos, e.pos):
                return
        op = e.rec.op
        assert isinstance(op, m.AlterModelTable)
        options = {**current.options, "db_table": op.table}
        if op.table is None:
            options.pop("db_table", None)
        names = solver._query_names(model)
        if not self._quiet(names, create, e, model):
            return
        self._commit("table", create, e, model, _create(current, options=options))

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


# Internal types for which some backend adds a CHECK constraint
# (data_type_check_constraints): created inline (unnamed, so named by the
# database) with the column, but by AlterField under Django's hashed name.
CHECK_TYPES = frozenset(
    {
        "PositiveBigIntegerField",
        "PositiveIntegerField",
        "PositiveSmallIntegerField",
        "JSONField",
        "BooleanField",
        "NullBooleanField",
    }
)


def _check_kind(field: Any) -> Any:
    cls = type(field)
    if not cls.__module__.startswith("django.db.models"):
        # A custom field may add its own check: assume it depends on the class.
        return cls
    internal = field.get_internal_type()
    return internal if internal in CHECK_TYPES else None


def _rename_keeps_names(field: Any) -> bool:
    """
    Whether creating ``field`` under its new name gives the database the same
    object names as creating it under the old name and renaming it: no index,
    unique or check constraint, foreign key or many-to-many table derives a
    name from the column or field name, or the column is fixed by db_column.
    """
    if getattr(field, "many_to_many", False):
        return False
    if getattr(field, "db_column", None):
        return True
    return not (
        getattr(field, "remote_field", None) is not None
        or field.unique
        or field.db_index
        or field.primary_key
        or _check_kind(field) is not None
        or getattr(field, "generated", False)
    )


def _mentions(value: Any, name: str) -> bool:
    """Whether a name appears in serialized options (ordering, indexes...)."""
    from django.db.migrations.writer import MigrationWriter

    try:
        text = MigrationWriter.serialize(value)[0]
    except Exception:
        return True
    return re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text) is not None


def same_named_objects(a: Any, b: Any) -> bool:
    """
    Whether going from definition ``a`` to ``b`` by AlterField creates no
    database object whose name would differ had ``b`` been created directly.
    A column's UNIQUE and CHECK constraints are inline (database-named) when
    the column is created, but added by AlterField under Django's hashed
    names (..._uniq, ..._check). Indexes, "like" indexes and foreign keys
    get the same Django-generated names either way.
    """
    if bool(getattr(a, "unique", False)) != bool(getattr(b, "unique", False)):
        return False
    if _check_kind(a) != _check_kind(b):
        return False
    return not any(type(f).__name__ == "GeneratedField" for f in (a, b))


def referenced_by_fields(life: FieldLife) -> bool:
    """Whether any field definition (a foreign key's to_field) points here."""
    return any(
        type(e.rec.op) in (m.AddField, m.AlterField, m.CreateModel) and not e.opaque
        for e in life.referenced_by
    )
