"""
Mandatory verification: the edited history must replay without error, never
leave more relations dangling than the original at any migration boundary,
and produce exactly the same final project state.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import TYPE_CHECKING, Any

from django.db.migrations import operations as m
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ModelState, ProjectState
from django.db.migrations.utils import resolve_relation
from django.db.migrations.writer import MigrationWriter

from django_clean_migrations.history import Key, generate_plan
from django_clean_migrations.locate import TOGETHER_OPTIONS, normalize_together
from django_clean_migrations.ops import INERT, OPAQUE, classify, isolated

if TYPE_CHECKING:
    from django_clean_migrations.history import History

_MODEL_NAME_OPS = frozenset(
    {
        "CreateModel",
        "DeleteModel",
        "AlterModelTable",
        "AlterModelTableComment",
        "AlterModelOptions",
        "AlterModelManagers",
        "AlterUniqueTogether",
        "AlterIndexTogether",
        "AlterOrderWithRespectTo",
    }
)


def _relation_targets(ms: ModelState) -> set[Key]:
    targets: set[Key] = set()
    for f in ms.fields.values():
        remote = getattr(f, "remote_field", None)
        if remote is None:
            continue
        for model in (remote.model, getattr(remote, "through", None)):
            if model is None:
                continue
            try:
                app, name = resolve_relation(model, ms.app_label, ms.name_lower)
            except (TypeError, ValueError, AttributeError):
                continue
            targets.add((app, name.lower()))
    return targets


class DanglingTracker:
    """
    Incrementally maintains the set of models referenced by relation fields
    but absent from the state, so boundary checks stay cheap on long histories.
    """

    def __init__(self) -> None:
        self.targets: dict[Key, set[Key]] = {}
        self.referrers: dict[Key, set[Key]] = {}
        self.missing: set[Key] = set()

    def before(
        self, op: Operation, app_label: str, state: ProjectState
    ) -> set[Key] | None:
        """Model keys whose fields ``op`` may change; None means unknown."""
        cls = type(op)
        if cls is m.RunPython:
            return set()
        if cls is m.RunSQL or cls is m.SeparateDatabaseAndState:
            touched: set[Key] = set()
            for inner in op.state_operations:  # type: ignore[attr-defined]
                sub = self.before(inner, app_label, state)
                if sub is None:
                    return None
                touched |= sub
            return touched
        kind = classify(op)
        if kind in (INERT, "ValidateConstraint"):
            return set()
        if kind == OPAQUE:
            return None
        if kind == "RenameModel":
            old = (app_label, op.old_name.lower())  # type: ignore[attr-defined]
            new = (app_label, op.new_name.lower())  # type: ignore[attr-defined]
            return {old, new} | self.referrers.get(old, set())
        if kind in _MODEL_NAME_OPS:
            return {(app_label, op.name.lower())}  # type: ignore[attr-defined]
        return {(app_label, op.model_name.lower())}  # type: ignore[attr-defined]

    def after(self, touched: set[Key] | None, state: ProjectState) -> None:
        keys = set(state.models) | set(self.targets) if touched is None else touched
        changed: set[Key] = set()
        for key in keys:
            old = self.targets.pop(key, set())
            ms = state.models.get(key)
            new = _relation_targets(ms) if ms is not None else set()
            for t in old - new:
                refs = self.referrers[t]
                refs.discard(key)
                if not refs:
                    del self.referrers[t]
                changed.add(t)
            for t in new - old:
                self.referrers.setdefault(t, set()).add(key)
                changed.add(t)
            if new:
                self.targets[key] = new
            changed.add(key)
        for t in changed:
            if t in self.referrers and t not in state.models:
                self.missing.add(t)
            else:
                self.missing.discard(t)


def _normalized_options(options: Mapping[str, Any]) -> dict[str, Any]:
    # Django treats a missing, None, empty, list-of-lists or set-of-tuples
    # together option identically (normalize_together / autodetector).
    result = dict(options)
    for option in TOGETHER_OPTIONS:
        value = result.pop(option, None)
        if value:
            result[option] = frozenset(normalize_together(value))
    return result


def same_value(a: Any, b: Any) -> bool:
    """
    Equality that also holds for separately loaded copies of deconstructible
    objects lacking __eq__ (validators, storages...), by comparing how the
    migration writer would serialize them.
    """
    try:
        if a == b:
            return True
    except Exception:
        pass
    try:
        return bool(MigrationWriter.serialize(a) == MigrationWriter.serialize(b))
    except Exception:
        return False


def model_states_equal(a: ModelState, b: ModelState) -> bool:
    if (
        a.app_label != b.app_label
        or a.name != b.name
        or sorted(a.fields) != sorted(b.fields)
    ):
        return False
    for name, f1 in a.fields.items():
        f2 = b.fields[name]
        if type(f1) is not type(f2) or not same_value(
            f1.deconstruct()[1:], f2.deconstruct()[1:]
        ):
            return False
    oa, ob = _normalized_options(a.options), _normalized_options(b.options)
    return (
        oa.keys() == ob.keys()
        and all(same_value(oa[k], ob[k]) for k in oa)
        and same_value(a.bases, b.bases)
        and same_value(a.managers, b.managers)
    )


def first_difference(
    expected: Mapping[Key, ModelState], actual: Mapping[Key, ModelState]
) -> str | None:
    for key in sorted(expected.keys() | actual.keys()):
        if key not in actual:
            return f"model {key[0]}.{key[1]} is missing"
        if key not in expected:
            return f"model {key[0]}.{key[1]} is unexpected"
        if not model_states_equal(expected[key], actual[key]):
            return f"model {key[0]}.{key[1]} differs"
    return None


class VerificationError(Exception):
    pass


def verify_in_memory(
    history: History,
    edited: Mapping[int, Sequence[Operation]],
    original_dangling: Mapping[int, frozenset[Key]],
    original_final: ProjectState,
) -> None:
    state = ProjectState(real_apps=set(history.loader.unmigrated_apps))
    tracker = DanglingTracker()
    for info in history.migrations:
        app_label = info.app_label
        for op in edited.get(info.index, info.migration.operations):
            touched = tracker.before(op, app_label, state)
            try:
                isolated(op).state_forwards(app_label, state)
            except Exception as exc:
                raise VerificationError(
                    f"the edited history fails in {info.label}: {exc!r}"
                ) from exc
            tracker.after(touched, state)
        extra = tracker.missing - original_dangling.get(info.index, frozenset())
        if extra:
            names = ", ".join(sorted(f"{a}.{n}" for a, n in extra))
            raise VerificationError(
                f"the edited history leaves relations to missing model(s) {names}"
                f" after {info.label}"
            )
    diff = first_difference(original_final.models, state.models)
    if diff is not None:
        raise VerificationError(f"the edited history ends in a different state: {diff}")
    if list(original_final.models) != list(state.models):
        # post_migrate creates content types and permissions in this order:
        # a different order numbers their rows differently.
        raise VerificationError("the edited history lists the models in another order")


def state_from_disk() -> tuple[MigrationLoader, list[Key], ProjectState]:
    """Load the migrations currently on disk and replay them."""
    loader = MigrationLoader(None, ignore_no_migrations=True)
    plan = generate_plan(loader.graph)
    state = ProjectState(real_apps=set(loader.unmigrated_apps))
    for key in plan:
        state = loader.graph.nodes[key].mutate_state(state, preserve=False)
    return loader, plan, state


def verify_on_disk(
    original_plan: Sequence[Key],
    original_final: ProjectState,
    expected_ops: Mapping[Key, Sequence[str]],
    pruned: AbstractSet[Key] = frozenset(),
    ancestors: Mapping[Key, AbstractSet[Key]] | None = None,
    ends: Mapping[str, tuple[frozenset[Key], frozenset[Key]]] | None = None,
) -> None:
    """
    Reload the migrations from disk and check them. With ``pruned``
    migrations deleted, the plan may be ordered differently, but must hold
    the others, and each must have exactly ``ancestors`` (its old ancestors
    minus the pruned) as its ancestors.
    """
    loader, plan, state = state_from_disk()
    if not pruned:
        if plan != list(original_plan):
            raise VerificationError("the migration graph changed after rewriting")
    else:
        from django_clean_migrations.prune import ancestor_sets

        loader.graph.validate_consistency()
        expected_keys = [k for k in original_plan if k not in pruned]
        if sorted(plan) != sorted(expected_keys):
            raise VerificationError(
                "the migration graph doesn't hold exactly the migrations that"
                " were not pruned"
            )
        if ancestors is not None:
            found = ancestor_sets(loader.graph, plan)
            for key in plan:
                if found[key] != ancestors.get(key):
                    raise VerificationError(
                        f"after pruning, {key[0]}.{key[1]} doesn't run after the"
                        " same migrations as before"
                    )
        if ends is not None:
            from django_clean_migrations.prune import PruneError, check_ends

            try:
                check_ends(ends, loader.graph, pruned)
            except PruneError as exc:
                raise VerificationError(str(exc)) from exc
        for app in {k[0] for k in pruned}:
            before = {k for k in original_plan if k[0] == app and k not in pruned}
            if before and not loader.graph.leaf_nodes(app):
                raise VerificationError(f"app {app} has no migrations left")
    for key, names in expected_ops.items():
        actual = [type(op).__name__ for op in loader.graph.nodes[key].operations]
        if actual != list(names):
            raise VerificationError(
                f"{key[0]}.{key[1]} has operations {actual}, expected {list(names)}"
            )
    diff = first_difference(original_final.models, state.models)
    if diff is not None:
        raise VerificationError(
            f"the rewritten files produce a different state: {diff}"
        )
