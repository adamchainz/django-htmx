"""
Pruning: deleting migrations left with no operations, and rewiring the
migrations that depend on them (--prune).

A pruned migration P disappears from the graph; every migration that
depended on P depends instead on P's own dependencies (through chains of
pruned migrations), unless it already depends on them through its other
dependencies. So every surviving migration keeps exactly the same ancestors,
minus the pruned ones: the order constraints between the migrations that
do something are unchanged.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db.migrations.graph import MigrationGraph
from django.db.migrations.operations.base import Operation

from django_clean_migrations.history import (
    History,
    Key,
    MigrationInfo,
    SourceFile,
    generate_plan,
)

if TYPE_CHECKING:
    from django_clean_migrations.solver import Scope


@dataclass
class PrunePlan:
    pruned: list[Key] = field(default_factory=list)
    """Deleted migrations, in plan order."""
    parents: dict[Key, list[Key]] = field(default_factory=dict)
    """Surviving migration -> its new dependencies (resolved keys), for each
    migration whose dependencies change."""
    added: dict[Key, list[Key]] = field(default_factory=dict)
    """Surviving migration -> dependencies written into its file."""
    removed: dict[Key, list[Key]] = field(default_factory=dict)
    """Surviving migration -> dependencies taken out of its file."""
    dependants: dict[Key, list[Key]] = field(default_factory=dict)
    """Pruned migration -> the surviving migrations rewired because of it."""
    blocked: dict[Key, str] = field(default_factory=dict)
    """Empty migrations that are kept, with why."""

    def __bool__(self) -> bool:
        return bool(self.pruned)


class PruneError(Exception):
    pass


def plan_prune(
    history: History,
    final_ops: Mapping[int, Sequence[Operation]],
    scope: Scope,
) -> PrunePlan:
    """
    Which migrations with no operations left (``final_ops``: plan index ->
    operations, for the migrations whose operations change) can be deleted.
    """
    graph = history.loader.graph
    by_key = history.by_key
    plan = PrunePlan()
    candidates: list[MigrationInfo] = []
    for info in history.migrations:
        info = info.on_disk
        ops = final_ops.get(info.index, info.migration.operations)
        if not ops:
            candidates.append(info)
    if not candidates:
        return plan

    replaced: set[Key] = set()
    run_before_targets: dict[Key, Key] = {}
    for key, migration in history.loader.disk_migrations.items():
        replaced.update(tuple(r) for r in getattr(migration, "replaces", ()) or ())
        for target in getattr(migration, "run_before", ()) or ():
            run_before_targets[(target[0], target[1])] = key
        if key not in by_key:
            # Not in the graph (replaced by a squashed migration), but the
            # loader still resolves its dependencies: it would lose one.
            for dep in migration.dependencies:
                run_before_targets.setdefault((dep[0], dep[1]), key)
    mentions = _MentionIndex(history)

    for info in candidates:
        reason = _static_block(info, graph, scope, replaced, run_before_targets)
        if reason is None:
            other = mentions.mentioned_by(info)
            if other is not None:
                reason = f"its module is mentioned (imported?) by {other}"
        if reason is not None:
            plan.blocked[info.key] = reason
    pruned = {i.key for i in candidates if i.key not in plan.blocked}

    # Dependants must be rewritable; blocking one migration can make another
    # one's dependants survivors, so repeat until nothing changes.
    literal_deps: dict[Key, dict[Key, ast.expr] | None] = {}

    def literals(key: Key) -> dict[Key, ast.expr] | None:
        if key not in literal_deps:
            literal_deps[key] = _literal_dependencies(by_key[key])
        return literal_deps[key]

    def settle(chosen: set[Key]) -> PrunePlan:
        """The prune plan for ``chosen``, minus what must be kept."""
        attempt = PrunePlan(blocked=dict(plan.blocked))
        remaining = set(chosen)
        while True:
            _block_unrewritable(attempt, remaining, graph, by_key, scope, literals)
            # A dependency the dependants need copied must be a plain tuple
            # in the pruned file (not a swappable or __first__ one).
            if remaining and _rewire(attempt, remaining, history, literals):
                continue
            break
        if not remaining:
            attempt.dependants, attempt.parents = {}, {}
            attempt.added, attempt.removed = {}, {}
        attempt.pruned = sorted(remaining, key=lambda k: by_key[k].index)
        return attempt

    result = settle(pruned)
    if result.pruned and not _same_order(history, result):
        # migrate must still run the others in exactly the same order: two
        # unordered migrations that swap could, say, add columns to a table
        # in another order. Keep adding migrations to prune, in plan order,
        # as long as the order holds.
        accepted: set[Key] = set()
        best = settle(accepted)
        for key in result.pruned:
            attempt = settle(accepted | {key})
            if key in attempt.pruned and _same_order(history, attempt):
                accepted = set(attempt.pruned)
                best = attempt
        for key in result.pruned:
            if key not in best.pruned and key not in best.blocked:
                best.blocked[key] = (
                    "deleting it would change the order migrate runs the other"
                    " migrations in"
                )
        result = best
    if result.pruned:
        check_ancestors(history, result)
    return result


def _same_order(history: History, plan: PrunePlan) -> bool:
    """Whether Django's plan (MigrationGraph's depth-first walk, following
    each migration's sorted dependencies) is the old one minus the pruned."""
    pruned = set(plan.pruned)
    old = [k for k in history.plan if k not in pruned]
    return generate_plan(pruned_graph(history.loader.graph, plan)) == old


def _block_unrewritable(
    plan: PrunePlan,
    pruned: set[Key],
    graph: MigrationGraph,
    by_key: Mapping[Key, MigrationInfo],
    scope: Scope,
    literals: Callable[[Key], dict[Key, ast.expr] | None],
) -> None:
    """Block pruned migrations whose dependants can't be rewritten; blocking
    one can make another's dependants survivors, so until nothing changes."""
    changed = True
    while changed:
        changed = False
        for key in sorted(pruned, key=lambda k: by_key[k].index):
            reason = None
            for child in graph.node_map[key].children:
                if child.key in pruned:
                    continue
                child_info = by_key[child.key]
                if not child_info.editable:
                    reason = f"{child_info.label} depends on it and can't be edited"
                elif scope.app_excluded(child.key[0]):
                    reason = f"{child_info.label} depends on it, in an excluded app"
                else:
                    found = literals(child.key)
                    if found is None or key not in found:
                        reason = (
                            f"the dependency of {child_info.label} on it can't be"
                            " rewritten"
                        )
                if reason is not None:
                    break
            if reason is not None:
                plan.blocked[key] = reason
                pruned.discard(key)
                changed = True


def _rewire(
    plan: PrunePlan,
    pruned: set[Key],
    history: History,
    literals: Callable[[Key], dict[Key, ast.expr] | None],
) -> bool:
    """
    Compute the dependants' new dependencies. Returns True if a pruned
    migration had to be blocked instead (then solve again).
    """
    graph = history.loader.graph
    by_key = history.by_key
    ancestors = history.ancestors
    plan.dependants, plan.parents, plan.added, plan.removed = {}, {}, {}, {}
    for key in sorted(pruned, key=lambda k: by_key[k].index):
        for child in graph.node_map[key].children:
            if child.key not in pruned:
                plan.dependants.setdefault(key, []).append(child.key)
    for child_key in sorted(
        {c for cs in plan.dependants.values() for c in cs},
        key=lambda k: by_key[k].index,
    ):
        old_parents = [p.key for p in graph.node_map[child_key].parents]
        kept = [p for p in old_parents if p not in pruned]
        covered = 0
        for p in kept:
            index = by_key[p].index
            covered |= ancestors[index] | (1 << index)
        added: list[Key] = []
        for p in old_parents:
            if p not in pruned:
                continue
            for q, via in _effective_parents(p, graph, pruned):
                index = by_key[q].index
                # Django finds an app's first and latest migrations by
                # same-app edges only (root_nodes(), leaf_nodes(), conflict
                # detection): a migration that depended on a pruned one of
                # its app keeps a dependency on each nearest surviving one,
                # even when it already runs after it through other apps.
                same_app = p[0] == child_key[0] == q[0]
                if q in kept or q in added:
                    continue
                if covered >> index & 1 and not same_app:
                    continue
                if q not in (literals(via) or {}):
                    plan.blocked[via] = (
                        f"{child_key[0]}.{child_key[1]} would need its dependency"
                        f" on {q[0]}.{q[1]}, which isn't a plain tuple there"
                        " (swappable_dependency?)"
                    )
                    pruned.discard(via)
                    return True
                added.append(q)
                covered |= ancestors[index] | (1 << index)
        plan.parents[child_key] = kept + added
        plan.added[child_key] = added
        plan.removed[child_key] = [p for p in old_parents if p in pruned]
    return False


def _static_block(
    info: MigrationInfo,
    graph: MigrationGraph,
    scope: Scope,
    replaced: set[Key],
    run_before_targets: Mapping[Key, Key],
) -> str | None:
    migration = info.migration
    app = info.app_label
    if not info.editable or info.source is None or info.path is None:
        return "it is not editable (installed package)"
    if app not in scope.apps or scope.app_excluded(app):
        return f"app {app!r} is not selected"
    if getattr(migration, "replaces", None):
        return "it is a squashed migration"
    if info.key in replaced:
        return "a squashed migration replaces it"
    if info.key in graph.leaf_nodes(app):
        return "it is the latest migration of its app"
    node = graph.node_map[info.key]
    if migration.initial or not any(p.key[0] == app for p in node.parents):
        return "it is the first migration of its app"
    if migration.run_before:
        return "it has run_before"
    if info.key in run_before_targets:
        other = run_before_targets[info.key]
        return f"{other[0]}.{other[1]} (run_before, or replaced) refers to it"
    if info.source.tree is None:
        return "its source can't be parsed"
    return None


def _effective_parents(
    key: Key, graph: MigrationGraph, pruned: set[Key]
) -> list[tuple[Key, Key]]:
    """The dependencies of pruned ``key``, through chains of pruned
    migrations, each with the pruned migration that has it."""
    result: list[tuple[Key, Key]] = []
    seen: set[Key] = set()
    stack = [(p.key, key) for p in graph.node_map[key].parents]
    stack.reverse()
    while stack:
        k, via = stack.pop()
        if k in seen:
            continue
        seen.add(k)
        if k in pruned:
            more = [(p.key, k) for p in graph.node_map[k].parents]
            stack.extend(reversed(more))
        else:
            result.append((k, via))
    return result


class _MentionIndex:
    """Which migration files mention another migration's name, other than
    in their dependencies (an import_module() call, a string...)."""

    def __init__(self, history: History) -> None:
        self.files: list[tuple[MigrationInfo, str]] = []
        for info in history.migrations:
            info = info.on_disk
            if info.source is not None:
                self.files.append((info, info.source.text))
        self._names: dict[str, list[str]] = {}

    def _dependency_names(self, info: MigrationInfo) -> list[str]:
        if info.label not in self._names:
            literal = _literal_dependencies(info) or {}
            self._names[info.label] = [k[1] for k in literal]
        return self._names[info.label]

    def mentioned_by(self, target: MigrationInfo) -> str | None:
        word = re.compile(rf"(?<![\w]){re.escape(target.name)}(?![\w])")
        for info, text in self.files:
            if info.label == target.label:
                continue
            count = len(word.findall(text))
            if count and count > self._dependency_names(info).count(target.name):
                return info.label
        return None


def _migration_class(tree: ast.Module) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            return node
    return None


def _dependencies_node(info: MigrationInfo) -> ast.List | ast.Tuple | None:
    source = info.on_disk.source
    if source is None or source.tree is None:
        return None
    cls = _migration_class(source.tree)
    if cls is None:
        return None
    found: ast.List | ast.Tuple | None = None
    for stmt in cls.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets, value = [stmt.target], stmt.value
        if any(isinstance(t, ast.Name) and t.id == "dependencies" for t in targets):
            if found is not None or not isinstance(value, (ast.List, ast.Tuple)):
                return None
            found = value
    return found


def _literal_key(node: ast.expr) -> Key | None:
    if (
        isinstance(node, ast.Tuple | ast.List)
        and len(node.elts) == 2
        and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts
        )
    ):
        a, b = node.elts
        assert isinstance(a, ast.Constant) and isinstance(b, ast.Constant)
        return (str(a.value), str(b.value))
    return None


def _literal_dependencies(info: MigrationInfo) -> dict[Key, ast.expr] | None:
    """Literal ("app", "name") entries of the dependencies list, by key."""
    container = _dependencies_node(info)
    if container is None:
        return None
    return {key: e for e in container.elts if (key := _literal_key(e)) is not None}


def rewrite_dependencies(
    text: str, info: MigrationInfo, remove: Iterable[Key], add: Iterable[Key]
) -> str:
    """``text`` (the file, possibly already edited) with its dependencies
    list regenerated: ``remove`` entries dropped, ``add`` entries appended."""
    source = SourceFile(info.path or info.on_disk.path, text)  # type: ignore[arg-type]
    fake = MigrationInfo(info.key, info.migration, info.index, info.path, True, source)
    container = _dependencies_node(fake)
    if container is None:
        raise PruneError(f"the dependencies of {info.label} can't be located")
    drop = set(remove)
    entries: list[str] = []
    for element in container.elts:
        if _literal_key(element) in drop:
            continue
        start, end = source.node_span(element)
        entries.append(text[start:end])
    entries.extend(f"({k[0]!r}, {k[1]!r})".replace("'", '"') for k in add)
    start, end = source.node_span(container)
    opening, closing = ("[", "]") if isinstance(container, ast.List) else ("(", ")")
    line_start = text.rfind("\n", 0, start) + 1
    indent = re.match(r"[ \t]*", text[line_start:start])
    base = indent.group(0) if indent else ""
    multiline = "\n" in text[start:end] or len(entries) > 1
    if not entries:
        rendered = opening + closing
    elif not multiline:
        rendered = opening + entries[0] + ("," if closing == ")" else "") + closing
    else:
        inner = base + "    "
        rendered = (
            opening + "\n" + "".join(f"{inner}{e},\n" for e in entries) + base + closing
        )
    return text[:start] + rendered + text[end:]


def check_ancestors(history: History, plan: PrunePlan) -> None:
    """
    Every survivor's ancestors must be the old ones minus the pruned, and
    every app's first and latest migrations (by same-app edges, as Django
    sees them) the old ones minus the pruned.
    """
    check_ends(
        history.loader.graph, pruned_graph(history.loader.graph, plan), plan.pruned
    )
    graph = history.loader.graph
    pruned = set(plan.pruned)
    pruned_mask = 0
    for key in pruned:
        pruned_mask |= 1 << history.by_key[key].index
    new: dict[Key, int] = {}
    for key in history.plan:
        if key in pruned:
            continue
        parents = plan.parents.get(key)
        if parents is None:
            parents = [p.key for p in graph.node_map[key].parents]
        mask = 0
        for p in parents:
            if p in pruned:
                raise PruneError(f"{key[0]}.{key[1]} still depends on pruned {p}")
            index = history.by_key[p].index
            mask |= new[p] | (1 << index)
        new[key] = mask
        expected = history.ancestors[history.by_key[key].index] & ~pruned_mask
        if mask != expected:
            raise PruneError(
                f"pruning would change the migrations {key[0]}.{key[1]} runs after"
            )


def ancestor_sets(
    graph: MigrationGraph, keys: Iterable[Key]
) -> dict[Key, frozenset[Key]]:
    """Strict ancestors of each of ``keys`` in ``graph``."""
    memo: dict[Key, frozenset[Key]] = {}

    def visit(key: Key) -> frozenset[Key]:
        if key in memo:
            return memo[key]
        stack = [(key, False)]
        while stack:
            k, ready = stack.pop()
            if k in memo:
                continue
            parents = [p.key for p in graph.node_map[k].parents]
            if ready:
                acc: set[Key] = set()
                for p in parents:
                    acc.add(p)
                    acc |= memo[p]
                memo[k] = frozenset(acc)
            else:
                stack.append((k, True))
                stack.extend((p, False) for p in parents if p not in memo)
        return memo[key]

    return {k: visit(k) for k in keys}


def pruned_graph(graph: MigrationGraph, plan: PrunePlan) -> MigrationGraph:
    """A copy of ``graph`` without the pruned migrations, rewired."""
    pruned = set(plan.pruned)
    new = MigrationGraph()
    for key, migration in graph.nodes.items():
        if key not in pruned:
            new.add_node(key, migration)
    for key in new.nodes:
        parents = plan.parents.get(key)
        if parents is None:
            parents = [p.key for p in graph.node_map[key].parents]
        for parent in parents:
            new.add_dependency(None, key, parent, skip_validation=True)
    new.validate_consistency()
    return new


def app_ends(graph: MigrationGraph) -> dict[str, tuple[frozenset[Key], frozenset[Key]]]:
    """Per app, its root and leaf migrations as Django finds them."""
    apps = {key[0] for key in graph.nodes}
    return {
        app: (frozenset(graph.root_nodes(app)), frozenset(graph.leaf_nodes(app)))
        for app in apps
    }


def check_ends(
    old: MigrationGraph | Mapping[str, tuple[frozenset[Key], frozenset[Key]]],
    new: MigrationGraph,
    pruned: Iterable[Key],
) -> None:
    """Roots and leaves of every app must be unchanged, minus the pruned (no
    new "conflicting migrations", no app losing its initial migration)."""
    gone = set(pruned)
    before = app_ends(old) if isinstance(old, MigrationGraph) else old
    after = app_ends(new)
    for app in sorted(before.keys() | after.keys()):
        roots, leaves = before.get(app, (frozenset(), frozenset()))
        new_roots, new_leaves = after.get(app, (frozenset(), frozenset()))
        if new_leaves != leaves - gone:
            raise PruneError(
                f"pruning would change the latest migrations of {app}:"
                f" {sorted(k[1] for k in new_leaves)}"
            )
        if new_roots != roots - gone:
            raise PruneError(
                f"pruning would change the first migrations of {app}:"
                f" {sorted(k[1] for k in new_roots)}"
            )
