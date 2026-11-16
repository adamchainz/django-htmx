"""Scope selection, the dead-lives fixpoint and the superseded-operation pass."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.db.migrations import operations as m
from django.db.migrations.operations.base import Operation

from django_compact_migrations.backends import describe
from django_compact_migrations.fold import (
    FOLD_KINDS,
    Fold,
    Folder,
    RegenerationCheck,
    mysql_safe_alters,
    occupied,
    referenced_by_fields,
    same_named_objects,
)
from django_compact_migrations.history import History
from django_compact_migrations.opaque import OpaqueIndex, OpaqueOp, Pos, TextBuilder
from django_compact_migrations.timeline import (
    ConstraintLife,
    Element,
    FieldLife,
    Life,
    ModelLife,
    Timeline,
    TogetherLife,
    backs_foreign_key,
)

Unit = Life | Element
"""What a trial can block: an entity's whole removal, or one dropped element."""

KINDS = ("model", "field", "index", "constraint", "together", "superseded", "fold")
OPAQUE_POLICIES = ("scan", "block", "ignore")


class SelectionError(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    app_label: str
    model: str | None = None
    field: str | None = None

    @classmethod
    def parse(cls, text: str, app_labels: Iterable[str]) -> Target:
        parts = text.split(".")
        if not 1 <= len(parts) <= 3 or not all(parts):
            raise SelectionError(
                f"Invalid target {text!r}: use app_label, app_label.Model or"
                " app_label.Model.field."
            )
        by_lower = {a.lower(): a for a in app_labels}
        app = by_lower.get(parts[0].lower())
        if app is None:
            raise SelectionError(f"No installed app with label {parts[0]!r}.")
        return cls(
            app,
            parts[1].lower() if len(parts) > 1 else None,
            parts[2].lower() if len(parts) > 2 else None,
        )

    def matches(self, life: Life) -> bool:
        model = life.model
        if model.app_label != self.app_label:
            return False
        if self.model is None:
            return True
        if not any(n.lower() == self.model for n in model.names):
            return False
        if self.field is None:
            return True
        return isinstance(life, FieldLife) and any(
            n.lower() == self.field for n in life.names
        )


@dataclass
class Scope:
    targets: Sequence[Target]
    exclude: Sequence[Target]
    kinds: frozenset[str]
    default_apps: frozenset[str]

    def selected(self, life: Life) -> bool:
        if self.targets:
            return any(t.matches(life) for t in self.targets)
        return life.app_label in self.default_apps

    def excluded(self, life: Life) -> bool:
        return any(t.matches(life) for t in self.exclude)

    def app_excluded(self, app_label: str) -> bool:
        return any(t.model is None and t.app_label == app_label for t in self.exclude)

    @property
    def apps(self) -> frozenset[str]:
        if self.targets:
            return frozenset(t.app_label for t in self.targets)
        return self.default_apps


@dataclass
class Plan:
    removed: set[Life] = field(default_factory=set)
    roots: set[Life] = field(default_factory=set)
    blocked: dict[Life, list[str]] = field(default_factory=dict)
    dropped: dict[Element, str] = field(default_factory=dict)
    """Elements removed individually, with the reason/kind."""
    superseded: list[tuple[Element, Element]] = field(default_factory=list)
    redundant: list[Element] = field(default_factory=list)
    elidable: list[tuple[Element, str | None, Life | None]] = field(
        default_factory=list
    )
    """Removed elidable ops, with the name that matched and the entity."""
    noop: list[tuple[Element, str]] = field(default_factory=list)
    """Removed data migrations that do nothing on a fresh database, with
    what a trial saw them do."""
    folds: list[Fold] = field(default_factory=list)
    regenerated: dict[int, Operation] = field(default_factory=dict)
    """rec.seq -> the operation that replaces it (a fold's target)."""


class Solver:
    def __init__(
        self,
        history: History,
        timeline: Timeline,
        scope: Scope,
        opaque_policy: str = "scan",
        remove_all_elidable: bool = False,
        *,
        aggressive: bool = False,
        trial_blocked: Mapping[str, str] | None = None,
        texts: TextBuilder | None = None,
        noop: Mapping[tuple[int, int], str] | None = None,
        backends: frozenset[str] = frozenset(),
        untrialled: Mapping[tuple[int, int], str] | None = None,
    ) -> None:
        """
        ``aggressive``: name-scan and soft-reference hits don't block but are
        recorded in ``at_risk``, for a trial run to confirm or refute.
        ``trial_blocked``: keys (see unit_key()) of removal units a trial run
        showed to be unsafe.
        ``noop``: (plan index, index on disk) of data migrations a trial run
        showed to do nothing on a fresh database, with what it saw: they
        are removed (drop_noop()) and don't count for the name scan.
        ``backends``: MySQL-family servers configured (see backends), whose
        history-dependent DDL keeps some removals and folds.
        ``untrialled``: (plan index, index on disk) of data migrations a
        trial can't run (see Analysis): they block even when aggressive.
        """
        self.untrialled: Mapping[tuple[int, int], str] = untrialled or {}
        self.backends = backends
        self.noop: dict[int, str] = {}
        for rec in history.ops:
            note = (noop or {}).get((rec.migration.index, rec.original_index))
            if note is not None:
                self.noop[rec.seq] = note
        self.history = history
        self.timeline = timeline
        self.scope = scope
        self.policy = opaque_policy
        self.remove_all_elidable = remove_all_elidable
        self.opaque = OpaqueIndex(
            history, [o for o in timeline.opaque if o.rec.seq not in self.noop], texts
        )
        self.opaque_elements: dict[int, Element] = {}
        for elements in timeline.elements.values():
            for e in elements:
                if e.opaque:
                    self.opaque_elements[e.rec.seq] = e
        self.plan = Plan()
        self._static: dict[Life, str | None] = {}
        self._query_names_cache: dict[ModelLife, set[str]] = {}
        self._elidable_hits: dict[Life, list[tuple[OpaqueOp, str]]] = {}
        self.aggressive = aggressive
        self.trial_blocked: Mapping[str, str] = trial_blocked or {}
        self.at_risk: dict[Unit, dict[int, OpaqueOp]] = {}

    def unit_key(self, unit: Unit) -> str:
        """
        A name for a removal unit that stays the same across fold rounds and
        trial runs (which re-analyse the history and so make new objects).
        """
        if isinstance(unit, Life):
            # Keyed by where it ends: folds can move where a life starts
            # (an AddField into a CreateModel), never where it ends.
            name = unit.names[0] if unit.names else ""
            if isinstance(unit, TogetherLife):
                name = f"{unit.option}:{','.join(f.names[0] for f in unit.fields)}"
            assert unit.end is not None  # removal candidates end
            end = f"{unit.end.migration.label}#{unit.end.original_index}"
            return f"{unit.kind}:{end}:{unit.model.names[0]}:{name}"
        return f"element:{_origin(unit)}"

    @property
    def fold_kinds(self) -> frozenset[str]:
        return frozenset(FOLD_KINDS) if "fold" in self.scope.kinds else frozenset()

    def solve_folds(self, can_regenerate: RegenerationCheck) -> None:
        folder = Folder(self, can_regenerate)
        self.plan.folds = folder.run()
        self.plan.regenerated = dict(folder.pending)

    # Element state

    def removed(self, e: Element) -> bool:
        if e in self.plan.dropped:
            return True
        owner = e.owner
        if owner is None:
            return False
        removed = self.plan.removed
        return owner in removed or (
            owner.parent is not None and owner.parent in removed
        )

    def between(self, pos: Pos, start: Pos, end: Pos) -> bool:
        """Whether ``pos`` may run between two positions (conservative)."""
        ancestors = self.history.ancestors
        descendants = self.history.descendants
        mig, idx = pos
        if mig == start[0]:
            if idx <= start[1]:
                return False
        elif ancestors[start[0]] >> mig & 1:
            return False
        if mig == end[0]:
            if idx >= end[1]:
                return False
        elif descendants[end[0]] >> mig & 1:
            return False
        return True

    # Opaque checks

    def _opaque_block(
        self,
        names: set[str],
        start: Pos,
        end: Pos | None,
        hits: list[tuple[OpaqueOp, str]] | None,
        known_from: Pos,
        unit: Unit | None = None,
    ) -> str | None:
        """
        Reason an opaque op in the window blocks, else None. When ``hits`` is
        given (an entity removal), elidable ops that mention a name and are
        certain to run within the entity's lifetime are collected there to be
        removed alongside, or block if they can't be. Any other elidable op
        (unordered relative to the entity, or nothing it could need is being
        removed) blocks like any opaque op: a word match is not proof that
        it is about this entity.

        In aggressive mode, what would block is recorded against ``unit``
        instead.
        """
        for o, name in self.opaque.mentioning(names, start, end, known_from):
            untrialled = self.untrialled.get(
                (o.rec.migration.index, o.rec.original_index)
            )
            record = self.aggressive and unit is not None and untrialled is None
            if (
                o.elidable
                and hits is not None
                and end is not None
                and self._strictly_inside(o.pos, start, end)
                # One acting on every model isn't about this entity.
                and not self.opaque.text(o).generic
            ):
                reason = o.rec.not_editable_reason()
                if reason is None and self.scope.app_excluded(
                    o.rec.migration.app_label
                ):
                    reason = f"app {o.rec.migration.app_label!r} is excluded"
                if reason is None:
                    hits.append((o, name))
                    continue
                if self.policy == "ignore":
                    continue
                if record:
                    assert unit is not None
                    self.at_risk.setdefault(unit, {})[o.rec.seq] = o
                    continue
                return (
                    f"elidable {o.rec.describe()} mentions {name!r} but cannot be"
                    f" removed: {reason}"
                )
            if record:
                assert unit is not None
                self.at_risk.setdefault(unit, {})[o.rec.seq] = o
                continue
            if self.policy == "scan" or (o.elidable and self.policy == "block"):
                generic = self.opaque.text(o).generic
                what = (
                    f"acts on every model ({generic})"
                    if generic is not None
                    else f"mentions {name!r}"
                )
                return f"{o.rec.describe()} {what}" + (
                    f" ({untrialled})" if untrialled else ""
                )
        if self.policy == "block" and not (self.aggressive and unit is not None):
            blocking = self.opaque.first_blocking(start, end, known_from)
            if blocking is not None:
                return f"opaque {blocking.rec.describe()} may run meanwhile"
        return None

    def _strictly_inside(self, pos: Pos, start: Pos, end: Pos) -> bool:
        """Whether ``pos`` runs after ``start`` and before ``end`` in every plan."""
        mig, idx = pos
        if mig == start[0]:
            after = idx > start[1]
        else:
            after = bool(self.history.ancestors[mig] >> start[0] & 1)
        if mig == end[0]:
            before = idx < end[1]
        else:
            before = bool(self.history.descendants[mig] >> end[0] & 1)
        return after and before

    def _static_blocker(self, life: Life) -> str | None:
        """Blockers that don't depend on which other lives are removed."""
        if life in self._static:
            return self._static[life]
        reason: str | None = self.trial_blocked.get(self.unit_key(life))
        if reason is not None:
            self._static[life] = reason
            return reason
        ordered = list(life.subtree())
        lives = set(ordered)
        for sub in ordered:
            if sub.poisoned:
                reason = sub.poison_reasons[0]
                if sub is not life:
                    reason = f"its {sub.kind} {_short_label(sub)} was {reason}"
                break
            if sub is not life and self.scope.excluded(sub):
                reason = f"it contains the excluded {sub.kind} {_short_label(sub)}"
                break
            if isinstance(sub, FieldLife) and sub.ever_pk and sub is life:
                reason = "it is or was a primary key"
                break
            for e in sub.elements:
                if e.sub is not None and self._primary_owner(e) in lives:
                    # The whole operation goes, its parts needn't be located.
                    r = e.rec.not_editable_reason()
                else:
                    r = e.not_editable_reason()
                if r is not None:
                    reason = r
                    break
            if reason:
                break
        if reason is None and self.backends:
            field = backs_foreign_key(life)
            if field is not None:
                reason = (
                    f"on {describe(self.backends)}, the index backing the foreign"
                    f" key {field.label} would get another name (the server"
                    f" drops it when this {life.kind} is created, Django creates"
                    " another one when it is removed)"
                )
        if reason is None:
            hits: list[tuple[OpaqueOp, str]] = []
            reason = self._opaque_block(
                _removal_names(life),
                life.start_pos,
                life.end_pos,
                hits,
                life.start_pos,
                life,
            )
            if reason is None:
                self._elidable_hits[life] = hits
        self._static[life] = reason
        return reason

    def _primary_owner(self, e: Element) -> Life | None:
        # Operations with parts register their whole-operation element first.
        whole = self.timeline.elements[e.rec.seq][0]
        assert whole.sub is None
        return whole.owner

    def _query_names(self, model: ModelLife) -> set[str]:
        """
        Names by which code may query ``model`` with its default ordering:
        its own scan names (names, tables, related names of its relations),
        those of models inheriting from it (which inherit Meta.ordering), and
        every name of relation fields elsewhere pointing at it. The latter
        only strictly matter for many-to-many and reverse managers, but
        forward foreign keys are included rather than relying on that.
        """
        cached = self._query_names_cache.get(model)
        if cached is None:
            cached = set()
            # Bases exist before the models listing them: no cycles, and
            # Django allows no diamonds of concrete models.
            todo = [model]
            while todo:
                current = todo.pop()
                cached |= current.scan_names()
                for f in current.incoming:
                    cached |= {n.lower() for n in f.names} | f.related_names
                todo.extend(current.child_models)
            self._query_names_cache[model] = cached
        return cached

    def _soft_blocker(self, life: FieldLife) -> str | None:
        if self.policy == "ignore":
            return None
        for soft in life.soft_refs:
            if self.removed(soft.element):
                continue
            end = soft.end if soft.end is not None else life.end_pos
            reason = self._opaque_block(
                self._query_names(life.model),
                soft.start,
                end,
                None,
                life.model.start_pos,
                life,
            )
            if reason is not None:
                return f"{soft.element.describe()} orders by it, and meanwhile {reason}"
        return None

    def blocker(self, life: Life) -> str | None:
        reason = self._static_blocker(life)
        if reason is not None:
            return reason
        for sub in life.subtree():
            for e in sub.referenced_by:
                if self.removed(e):
                    continue
                return f"referenced by {e.describe()}"
            if isinstance(sub, FieldLife):
                soft_reason = self._soft_blocker(sub)
                if soft_reason is not None:
                    return soft_reason
        return None

    # Pass 1

    def _candidate(self, life: Life) -> bool:
        return life.is_candidate and not self.scope.excluded(life)

    def _requirers(self, life: Life) -> Iterator[Life]:
        """Candidate lives that must be removed for ``life`` to be removable."""
        for sub in life.subtree():
            for e in sub.referenced_by:
                owner = e.owner
                if owner is None or self.removed(e):
                    continue
                if self._candidate(owner):
                    yield owner
                elif owner.parent is not None and self._candidate(owner.parent):
                    yield owner.parent

    def solve_dead(self) -> None:
        plan = self.plan
        for life in self.timeline.candidates():
            if (
                life.kind in self.scope.kinds
                and self.scope.selected(life)
                and not self.scope.excluded(life)
            ):
                plan.roots.add(life)
        plan.removed = set(plan.roots)
        queue = list(plan.roots)
        while queue:
            life = queue.pop()
            # (Lazily: an element of a life just added counts as removed.)
            for req in self._requirers(life):
                plan.removed.add(req)
                queue.append(req)
        while True:
            changed = True
            while changed:
                changed = False
                for life in sorted(plan.removed, key=lambda x: x.id):
                    reason = self.blocker(life)
                    if reason is not None:
                        plan.removed.discard(life)
                        plan.blocked.setdefault(life, []).append(reason)
                        changed = True
            required = self._required()
            if required == plan.removed:
                break
            plan.removed = required
        for life in sorted(plan.removed, key=lambda x: x.id):
            for o, name in self._elidable_hits.get(life, ()):
                element = self.opaque_elements[o.rec.seq]
                if element not in plan.dropped:
                    plan.dropped[element] = "elidable"
                    plan.elidable.append((element, name, life))

    def _required(self) -> set[Life]:
        """Removed lives reachable from a removed root."""
        plan = self.plan
        required = {r for r in plan.roots if r in plan.removed}
        queue = list(required)
        while queue:
            life = queue.pop()
            for sub in life.subtree():
                for e in sub.referenced_by:
                    owner = e.owner
                    # An opaque operation referencing it would block it.
                    assert owner is not None
                    for cand in (owner, owner.parent):
                        if (
                            cand is not None
                            and cand in plan.removed
                            and cand not in required
                        ):
                            required.add(cand)
                            queue.append(cand)
        return required

    # Elidable

    def drop_all_elidable(self) -> None:
        apps = self.scope.apps
        for o in self.timeline.opaque:
            if not o.elidable or o.rec.not_editable_reason() is not None:
                continue
            app_label = o.rec.migration.app_label
            if app_label not in apps or self.scope.app_excluded(app_label):
                continue
            element = self.opaque_elements[o.rec.seq]
            if element not in self.plan.dropped:
                self.plan.dropped[element] = "elidable"
                self.plan.elidable.append((element, None, None))

    # No-op data migrations

    def drop_noop(self) -> None:
        """Remove the data migrations a trial showed to do nothing; the name
        scan already ignores them (see __init__)."""
        for o in self.timeline.opaque:
            note = self.noop.get(o.rec.seq)
            if note is None:
                continue
            # The name scan ignores no-op data migrations: nothing has
            # dropped this one as an elidable mention yet.
            element = self.opaque_elements[o.rec.seq]
            self.plan.dropped[element] = "noop"
            self.plan.noop.append((element, note))

    # Redundant together operations

    def drop_redundant_together(self) -> None:
        removed = self.plan.removed
        for model in self.timeline.lives:
            if not isinstance(model, ModelLife) or model in removed:
                continue
            for events in model.together_events.values():
                self._redundant(events)

    def _redundant(self, events: list[Any]) -> None:
        removed = self.plan.removed
        before: frozenset[TogetherLife] = frozenset()
        before_orig: frozenset[TogetherLife] = frozenset()
        for event in events:
            orig = frozenset(event.lives)
            kept = frozenset(t for t in event.lives if t not in removed)
            if event.kind == "rename_index":
                before = before - kept
                before_orig = before_orig - orig
                continue
            if event.kind == "create":
                before, before_orig = kept, orig
                continue
            element: Element | None = event.element
            if (
                kept == before
                and orig != before_orig
                and element is not None
                and not self.removed(element)
                and element.not_editable_reason() is None
                and all(
                    self.history.ordered(
                        other.rec.migration.index, event.rec.migration.index
                    )
                    for other in events
                    if other is not event
                )
            ):
                self.plan.dropped[element] = "redundant"
                self.plan.redundant.append(element)
            else:
                before = kept
            before_orig = orig

    # Pass 2

    def solve_superseded(self) -> None:
        if "superseded" not in self.scope.kinds:
            return
        removed = self.plan.removed
        for life in self.timeline.lives:
            if life in removed or (life.parent is not None and life.parent in removed):
                continue
            if (
                life.poisoned
                or not self.scope.selected(life)
                or self.scope.excluded(life)
            ):
                continue
            if isinstance(life, FieldLife):
                self._superseded_fields(life)
            elif isinstance(life, ModelLife):
                self._superseded_model(life)

    def _drop_superseded(self, e1: Element, e2: Element) -> None:
        self.plan.dropped[e1] = "superseded"
        self.plan.superseded.append((e1, e2))

    def _window_clear(
        self, names: set[str], e1: Element, e2: Element, life: Life
    ) -> bool:
        return (
            self._opaque_block(names, e1.pos, e2.pos, None, life.start_pos, e1) is None
        )

    def rows_unchanged(self, model: ModelLife, pos: Pos, unit: Unit) -> bool:
        """
        Whether no data migration may have written rows of ``model`` before
        ``pos`` (since the model was created): existing rows go through an
        AlterField (a new default fills NULLs, a type change converts), so
        an AlterField folded or dropped at ``pos`` would give them other
        values.
        """
        return (
            self._opaque_block(
                model.scan_names(), model.start_pos, pos, None, model.start_pos, unit
            )
            is None
        )

    def _superseded_fields(self, life: FieldLife) -> None:
        if life.ever_pk:
            return
        own = sorted(life.elements, key=lambda e: e.rec.seq)
        defs = {id(e): (fld, target) for e, fld, target in life.defs}
        prev_def, prev_target = life.defs[0][1], life.defs[0][2]
        for i, e1 in enumerate(own):
            if type(e1.rec.op) is not m.AlterField or id(e1) not in defs:
                continue
            d1, t1 = defs[id(e1)]
            e2 = next(
                (
                    e
                    for e in own[i + 1 :]
                    if type(e.rec.op) in (m.AlterField, m.RemoveField)
                ),
                None,
            )
            if e2 is not None and self._can_drop_alter_field(
                life, own, i, e1, e2, (prev_def, prev_target), (d1, t1), defs
            ):
                self._drop_superseded(e1, e2)
            else:
                prev_def, prev_target = d1, t1

    def _can_drop_alter_field(
        self,
        life: FieldLife,
        own: list[Element],
        i: int,
        e1: Element,
        e2: Element,
        before: tuple[Any, Life | None],
        current: tuple[Any, Life | None],
        defs: dict[int, tuple[Any, Life | None]],
    ) -> bool:
        if (
            self.unit_key(e1) in self.trial_blocked
            or e1.not_editable_reason() is not None
            or self.removed(e2)
        ):
            return False
        if not self.history.ordered(e1.pos[0], e2.pos[0]):
            return False
        if any(e.info.get("rename") for e in own[i + 1 : own.index(e2)]):
            return False
        d0, t0 = before
        d1, t1 = current
        d2, t2 = defs.get(id(e2), (None, None))
        refs = {id(fld): set(e.refs) for e, fld, _ in life.defs}
        field_defs = [d for d in (d0, d1, d2) if d is not None]
        # (Never a primary key: see _superseded_fields.)
        if any(getattr(d, "many_to_many", False) for d in field_defs):
            return False
        if self.backends and not mysql_safe_alters(field_defs, self.backends):
            return False
        # Without E1, E2 makes the transition from d0: a UNIQUE or CHECK that
        # E1 added (or dropped and E2 re-added) would end up with another
        # name, and foreign keys pointing here would not be recreated (and
        # renamed) when E1 changes the field's type.
        if any(
            not same_named_objects(a, b)
            for a, b in zip(field_defs, field_defs[1:], strict=False)
        ) or referenced_by_fields(life):
            return False
        if d2 is not None and not (
            _same_kind(d0, t0, d1, t1) or _same_kind(d1, t1, d2, t2)
        ):
            return False
        # Without E1, d0 stays in force until E2: anything d0 occupies or
        # references that d1 doesn't would be held during E1..E2, where the
        # history may already reuse or remove it (a column name, a to_field,
        # a remote model...). State verification can't see a column clash.
        if occupied(d0) != occupied(d1) or refs.get(id(d0)) != refs.get(id(d1)):
            return False
        for e in life.referenced_by:
            if not self.removed(e) and self.between(e.pos, e1.pos, e2.pos):
                return False
        names = life.scan_names() | life.model.scan_names()
        if not self._window_clear(names, e1, e2, life):
            return False
        # E1 rewrites existing rows (NULLs filled with its default, values
        # converted to a new type): rows written before it must not exist.
        rewrites = type(d0) is not type(d1) or (
            getattr(d0, "null", False) and not getattr(d1, "null", False)
        )
        return not rewrites or self.rows_unchanged(life.model, e1.pos, e1)

    def _superseded_model(self, model: ModelLife) -> None:
        own = sorted(
            (e for e in model.elements if e.sub is None and not e.opaque),
            key=lambda e: e.rec.seq,
        )
        by_type: dict[type[Any], list[Element]] = {}
        for e in own:
            by_type.setdefault(type(e.rec.op), []).append(e)
        # AlterModelTable is deliberately absent: dropping one frees its
        # table name early, which another model may claim meanwhile.
        for cls in (
            m.AlterModelOptions,
            m.AlterModelManagers,
            m.AlterModelTableComment,
        ):
            chain = by_type.get(cls, [])
            for e1, e2 in zip(chain, chain[1:], strict=False):
                if self._can_drop_model_op(model, e1, e2):
                    self._drop_superseded(e1, e2)

    def _can_drop_model_op(self, model: ModelLife, e1: Element, e2: Element) -> bool:
        if (
            self.unit_key(e1) in self.trial_blocked
            or e1.not_editable_reason() is not None
            or self.removed(e2)
        ):
            return False
        if not self.history.ordered(e1.pos[0], e2.pos[0]):
            return False
        op = e1.rec.op
        if type(op) is m.AlterModelOptions:
            # (A model switching between managed and unmanaged is locked.)
            if not set(op.options) <= set(m.AlterModelOptions.ALTER_OPTION_KEYS):
                return False
        elif type(op) is m.AlterModelTableComment:
            for life in model.subtree():
                for e in [*life.elements, *life.referenced_by]:
                    if e is e1 or e is e2 or self.removed(e):
                        continue
                    if self.between(e.pos, e1.pos, e2.pos):
                        return False
        return self._window_clear(model.scan_names(), e1, e2, model)


def _origin(e: Element) -> str:
    rec = e.rec
    return f"{rec.migration.label}#{rec.original_index}"


def _short_label(life: Life) -> str:
    if isinstance(life, FieldLife):
        return life.label
    if isinstance(life, TogetherLife):
        names = ", ".join(f.names[-1] for f in life.fields)
        return f"{life.model.label} {life.option} ({names})"
    return f"{life.model.label} {life.names[0]!r}"


def _same_kind(a: Any, ta: Life | None, b: Any, tb: Life | None) -> bool:
    if type(a) is not type(b) or a.get_internal_type() != b.get_internal_type():
        return False
    if getattr(a, "remote_field", None) is not None:
        return ta is not None and ta is tb
    return True


def kinds_from(
    kinds: Collection[str] | None, skip: Collection[str] = ()
) -> frozenset[str]:
    unknown = (set(kinds or ()) | set(skip)) - set(KINDS)
    if unknown:
        raise SelectionError(f"Unknown kind(s): {', '.join(sorted(unknown))}")
    return frozenset(kinds or KINDS) - frozenset(skip)


def _removal_names(life: Life) -> set[str]:
    """
    The names whose mention by a data migration within ``life`` blocks its
    removal. A constraint or ``unique_together`` entry is enforced: a data
    migration writing the table may rely on it (``ignore_conflicts``, an
    IntegrityError caught) without naming it, so the model and the fields
    count too.
    """
    names = life.scan_names()
    if isinstance(life, ConstraintLife):
        assert life.creation is not None  # a candidate
        fields = [r for r in life.creation.refs if isinstance(r, FieldLife)]
    elif isinstance(life, TogetherLife) and life.option == "unique_together":
        fields = list(life.fields)
    else:
        return names
    names |= life.model.scan_names()
    for fld in fields:
        names |= fld.scan_names()
    return names
