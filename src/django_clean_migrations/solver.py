"""Scope selection, the dead-lives fixpoint and the superseded-operation pass."""

from __future__ import annotations

from collections.abc import Collection, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.db.migrations import operations as m

from django_clean_migrations.history import History
from django_clean_migrations.opaque import OpaqueIndex, OpaqueOp, Pos
from django_clean_migrations.timeline import (
    Element,
    FieldLife,
    Life,
    ModelLife,
    Timeline,
    TogetherLife,
)

KINDS = ("model", "field", "index", "constraint", "together", "superseded")
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
    elidable: list[Element] = field(default_factory=list)


class Solver:
    def __init__(
        self,
        history: History,
        timeline: Timeline,
        scope: Scope,
        opaque_policy: str = "scan",
        remove_all_elidable: bool = False,
    ) -> None:
        self.history = history
        self.timeline = timeline
        self.scope = scope
        self.policy = opaque_policy
        self.remove_all_elidable = remove_all_elidable
        self.opaque = OpaqueIndex(history, timeline.opaque)
        self.opaque_elements: dict[int, Element] = {}
        for elements in timeline.elements.values():
            for e in elements:
                if e.opaque:
                    self.opaque_elements[e.rec.seq] = e
        self.plan = Plan()
        self._static: dict[Life, str | None] = {}
        self._elidable_hits: dict[Life, list[OpaqueOp]] = {}

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

    def _between(self, e: Element, start: Pos, end: Pos) -> bool:
        """Whether ``e`` may run between two positions (conservative)."""
        anc = self.history.ancestors
        desc = self.history.descendants
        mig, idx = e.pos
        if mig == start[0]:
            if idx <= start[1]:
                return False
        elif anc[start[0]] >> mig & 1:
            return False
        if mig == end[0]:
            if idx >= end[1]:
                return False
        elif desc[end[0]] >> mig & 1:
            return False
        return True

    # Opaque checks

    def _opaque_block(
        self,
        names: set[str],
        start: Pos,
        end: Pos | None,
        hits: list[OpaqueOp] | None,
    ) -> str | None:
        """
        Reason an opaque op in the window blocks, else None. When ``hits`` is
        given (an entity removal), elidable ops that mention a name are
        collected there to be removed alongside, or block if they can't be.
        Otherwise (nothing they could need is removed) they block like any
        opaque op.
        """
        for o, name in self.opaque.mentioning(names, start, end):
            if o.elidable and hits is not None:
                reason = o.rec.not_editable_reason()
                if reason is None:
                    hits.append(o)
                    continue
                if self.policy == "ignore":
                    continue
                return (
                    f"elidable {o.rec.describe()} mentions {name!r} but cannot be"
                    f" removed: {reason}"
                )
            if self.policy == "scan" or (o.elidable and self.policy == "block"):
                return f"{o.rec.describe()} mentions {name!r}"
        if self.policy == "block":
            blocking = self.opaque.first_blocking(start, end)
            if blocking is not None:
                return f"opaque {blocking.rec.describe()} may run meanwhile"
        return None

    def _static_blocker(self, life: Life) -> str | None:
        """Blockers that don't depend on which other lives are removed."""
        if life in self._static:
            return self._static[life]
        reason: str | None = None
        lives = set(life.subtree())
        for sub in lives:
            if sub.poisoned:
                reason = sub.poison_reasons[0]
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
        if reason is None:
            hits: list[OpaqueOp] = []
            reason = self._opaque_block(
                life.scan_names(), life.start_pos, life.end_pos, hits
            )
            if reason is None:
                self._elidable_hits[life] = hits
        self._static[life] = reason
        return reason

    def _primary_owner(self, e: Element) -> Life | None:
        for other in self.timeline.elements[e.rec.seq]:
            if other.sub is None and not other.opaque:
                return other.owner
        return None

    def _soft_blocked(self, life: FieldLife) -> bool:
        if self.policy == "ignore":
            return False
        return (
            self._opaque_block(
                life.model.scan_names(), life.start_pos, life.end_pos, None
            )
            is not None
        )

    def blocker(self, life: Life) -> str | None:
        reason = self._static_blocker(life)
        if reason is not None:
            return reason
        lives = set(life.subtree())
        for sub in lives:
            for e in sub.referenced_by:
                if self.removed(e):
                    continue
                return f"referenced by {e.describe()}"
            if isinstance(sub, FieldLife):
                soft = [e for e in sub.soft_referenced_by if not self.removed(e)]
                if soft and self._soft_blocked(sub):
                    return (
                        f"{soft[0].describe()} orders by it and an opaque operation"
                        " may query the model meanwhile"
                    )
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
            for req in self._requirers(life):
                if req not in plan.removed:
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
        for life in plan.removed:
            for o in self._elidable_hits.get(life, ()):
                element = self.opaque_elements[o.rec.seq]
                if element not in plan.dropped:
                    plan.dropped[element] = "elidable"
                    plan.elidable.append(element)

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
                    if owner is None:
                        continue
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
            if o.rec.migration.app_label not in apps:
                continue
            element = self.opaque_elements[o.rec.seq]
            if element not in self.plan.dropped:
                self.plan.dropped[element] = "elidable"
                self.plan.elidable.append(element)

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

    def _window_clear(self, names: set[str], e1: Element, e2: Element) -> bool:
        return self._opaque_block(names, e1.pos, e2.pos, None) is None

    def _superseded_fields(self, life: FieldLife) -> None:
        if life.ever_pk:
            return
        own = sorted(life.elements, key=lambda e: e.rec.seq)
        defs = {id(e): (fld, target) for e, fld, target in life.defs}
        if not life.defs:
            return
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
        if e1.not_editable_reason() is not None or self.removed(e2):
            return False
        if not self.history.ordered(e1.pos[0], e2.pos[0]):
            return False
        if any(e.info.get("rename") for e in own[i + 1 : own.index(e2)]):
            return False
        d0, t0 = before
        d1, t1 = current
        d2, t2 = defs.get(id(e2), (None, None))
        field_defs = [d for d in (d0, d1, d2) if d is not None]
        if any(getattr(d, "many_to_many", False) for d in field_defs):
            return False
        if len({bool(getattr(d, "primary_key", False)) for d in field_defs}) > 1:
            return False
        if d2 is not None and not (
            _same_kind(d0, t0, d1, t1) or _same_kind(d1, t1, d2, t2)
        ):
            return False
        # During E1..E2 the field would point at d0's model instead of d1's.
        if (
            getattr(d0, "remote_field", None) is not None
            and t0 is not t1
            and (t0 is None or (t0.end is not None and not self._ends_after(t0, e2)))
        ):
            return False
        for e in life.referenced_by:
            if not self.removed(e) and self._between(e, e1.pos, e2.pos):
                return False
        names = life.scan_names() | life.model.scan_names()
        return self._window_clear(names, e1, e2)

    def _ends_after(self, life: Life, e: Element) -> bool:
        end = life.end_pos
        assert end is not None
        if end[0] == e.pos[0]:
            return end[1] > e.pos[1]
        return bool(self.history.descendants[e.pos[0]] >> end[0] & 1)

    def _superseded_model(self, model: ModelLife) -> None:
        own = sorted(
            (e for e in model.elements if e.sub is None and not e.opaque),
            key=lambda e: e.rec.seq,
        )
        by_type: dict[type[Any], list[Element]] = {}
        for e in own:
            by_type.setdefault(type(e.rec.op), []).append(e)
        for cls in (
            m.AlterModelOptions,
            m.AlterModelManagers,
            m.AlterModelTable,
            m.AlterModelTableComment,
        ):
            chain = by_type.get(cls, [])
            for e1, e2 in zip(chain, chain[1:], strict=False):
                if self._can_drop_model_op(model, e1, e2):
                    self._drop_superseded(e1, e2)

    def _can_drop_model_op(self, model: ModelLife, e1: Element, e2: Element) -> bool:
        if e1.not_editable_reason() is not None or self.removed(e2):
            return False
        if not self.history.ordered(e1.pos[0], e2.pos[0]):
            return False
        op = e1.rec.op
        if type(op) is m.AlterModelOptions:
            if not set(op.options) <= set(m.AlterModelOptions.ALTER_OPTION_KEYS):
                return False
            if "managed_before" not in e1.info:
                return False
            if bool(op.options.get("managed", True)) != bool(e1.info["managed_before"]):
                return False
        elif type(op) in (m.AlterModelTable, m.AlterModelTableComment):
            for life in model.subtree():
                for e in [*life.elements, *life.referenced_by]:
                    if e is e1 or e is e2 or self.removed(e):
                        continue
                    if self._between(e, e1.pos, e2.pos):
                        return False
        return self._window_clear(model.scan_names(), e1, e2)


def _same_kind(a: Any, ta: Life | None, b: Any, tb: Life | None) -> bool:
    if type(a) is not type(b):
        return False
    try:
        if a.get_internal_type() != b.get_internal_type():
            return False
    except Exception:
        return False
    if getattr(a, "remote_field", None) is not None:
        return ta is not None and ta is tb
    return True


def kinds_from(kinds: Collection[str] | None) -> frozenset[str]:
    if not kinds:
        return frozenset(KINDS)
    unknown = set(kinds) - set(KINDS)
    if unknown:
        raise SelectionError(f"Unknown kind(s): {', '.join(sorted(unknown))}")
    return frozenset(kinds)
