from __future__ import annotations

import ast
import functools
import inspect
import re
import textwrap
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.db.migrations.operations import RunPython, RunSQL, SeparateDatabaseAndState
from django.db.migrations.operations.base import Operation

from django_compact_migrations.history import History, MigrationInfo, OpRecord
from django_compact_migrations.ops import OPAQUE, classify

Pos = tuple[int, int]
"""(plan index of migration, operation index)."""


@functools.cache
def _pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![^\W_]){re.escape(name)}(?![^\W_])")


GENERIC = re.compile(
    r"(?<![^\W_])(create_permissions|create_contenttypes|emit_post_migrate_signal"
    r"|update_contenttypes|get_for_models?)(?![^\W_])"
)
"""Calls that act on every model the app registry holds, or may (a loop over
``get_for_model``): they create content type and permission rows for the
models that existed when they ran."""


class Text:
    """Lower-cased text an opaque operation may act on, with cached matching."""

    def __init__(self, text: str, match_all: bool) -> None:
        self.text = text.lower()
        found = GENERIC.search(self.text)
        self.generic = found.group() if found is not None else None
        """A call that acts on every model there is, without naming them."""
        self.match_all = match_all or found is not None
        self._cache: dict[str, bool] = {}

    def mentions(self, name: str) -> bool:
        if self.match_all:
            return True
        name = name.lower()
        if not name:
            return False
        hit = self._cache.get(name)
        if hit is None:
            hit = name in self.text and _pattern(name).search(self.text) is not None
            self._cache[name] = hit
        return hit


@dataclass(eq=False)
class OpaqueOp:
    rec: OpRecord
    state_changed: bool
    _text: Text | None = field(default=None, repr=False)

    @property
    def pos(self) -> Pos:
        return (self.rec.migration.index, self.rec.index)

    @property
    def elidable(self) -> bool:
        """
        Exact RunPython/RunSQL declared elidable that changes no state: never
        blocks, and is removed instead when it mentions a removed entity.
        """
        op = self.rec.op
        return (
            (type(op) is RunPython or type(op) is RunSQL)
            and bool(op.elidable)
            and not self.state_changed
        )


class TextBuilder:
    """Texts of opaque operations; reusable across analyses of one history."""

    def __init__(self) -> None:
        self._base: dict[int, tuple[str, bool]] = {}
        self._texts: dict[int, Text] = {}

    def text(self, opaque: OpaqueOp) -> Text:
        if opaque._text is None:
            cached = self._texts.get(id(opaque.rec.op))
            if cached is None:
                info = opaque.rec.migration.on_disk
                base, match_all = self._migration_text(info)
                extra, extra_all = _extra_text(opaque.rec.op, info.path)
                cached = Text(base + "\n" + extra, match_all or extra_all)
                self._texts[id(opaque.rec.op)] = cached
            opaque._text = cached
        return opaque._text

    def _migration_text(self, info: MigrationInfo) -> tuple[str, bool]:
        cached = self._base.get(info.index)
        if cached is None:
            cached = _migration_text(info)
            self._base[info.index] = cached
        return cached


def _migration_text(info: MigrationInfo) -> tuple[str, bool]:
    source = info.source
    if source is None:
        if info.path is not None:
            try:
                return info.path.read_text(encoding="utf-8", errors="replace"), False
            except OSError:
                pass
        return "", True
    skip: list[ast.AST] = list(_metadata_statements(source.tree))
    if info.op_nodes is not None:
        skip.extend(
            node
            for node, op in zip(info.op_nodes, info.migration.operations, strict=True)
            if classify(op) != OPAQUE
        )
    return python_words(source.tree, skip), False


def python_words(tree: ast.AST, skip: Iterable[ast.AST] = ()) -> str:
    """
    The parts of Python source that can name a model, field, relation, table
    or index at run time: string and bytes constants (f-string literal parts
    included), attribute names, keyword argument names, and imported module
    paths and names. Local variables, parameters, function and class names,
    and comments can't, so they are left out. Dynamic names (getattr,
    **kwargs) are in practice built from string constants in the same source.
    """
    skipped = {id(n) for n in skip}
    out: list[str] = []
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if id(node) in skipped:
            continue
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                out.append(node.value)
            elif isinstance(node.value, bytes):
                out.append(node.value.decode("utf-8", "replace"))
        elif isinstance(node, ast.Attribute):
            out.append(node.attr)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            out.append(node.arg)
        elif isinstance(node, ast.alias):
            out.append(node.name)
            if node.asname:
                out.append(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
        elif _is_get_model(node):
            # apps.get_model("app_label", "Model"): the first argument only
            # names an app, so don't let it match fields or models.
            assert isinstance(node, ast.Call)
            stack.extend([node.func, *node.args[1:], *node.keywords])
            continue
        stack.extend(ast.iter_child_nodes(node))
    return "\n".join(out)


def _is_get_model(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_model"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    )


def _source_words(source: str) -> str:
    """Words of a Python snippet, or the raw text if it doesn't parse."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except (SyntaxError, ValueError):
        return source
    return python_words(tree)


# Migration attributes that only name migrations: auto-generated names such as
# "0002_item_owner" would otherwise match model and field names.
_METADATA = frozenset({"dependencies", "replaces", "run_before", "initial", "atomic"})


def _metadata_statements(tree: ast.Module) -> list[ast.stmt]:
    found: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Migration":
            for stmt in node.body:
                targets: list[ast.expr] = []
                if isinstance(stmt, ast.Assign):
                    targets = stmt.targets
                elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                    targets = [stmt.target]
                if targets and all(
                    isinstance(t, ast.Name) and t.id in _METADATA for t in targets
                ):
                    found.append(stmt)
    return found


def _callable_source(func: Any, migration_path: Path | None) -> tuple[str, bool]:
    while isinstance(func, functools.partial):
        func = func.func
    if func is None or func is RunPython.noop:
        return "", False
    try:
        source_file = inspect.getsourcefile(func)
    except TypeError:
        source_file = None
    if (
        source_file is not None
        and migration_path is not None
        and Path(source_file).resolve() == migration_path.resolve()
    ):
        return "", False
    try:
        return _source_words(inspect.getsource(func)), False
    except (OSError, TypeError):
        return "", True


def _nested_ops(op: Operation) -> Iterator[Operation]:
    yield op
    if type(op) is SeparateDatabaseAndState:
        for inner in [*op.database_operations, *op.state_operations]:
            yield from _nested_ops(inner)
    elif type(op) is RunSQL:
        for inner in op.state_operations:
            yield from _nested_ops(inner)


def _extra_text(op: Operation, migration_path: Path | None) -> tuple[str, bool]:
    parts: list[str] = []
    match_all = False
    for inner in _nested_ops(op):
        if type(inner) is RunPython:
            for func in (inner.code, inner.reverse_code):
                text, unknown = _callable_source(func, migration_path)
                parts.append(text)
                match_all |= unknown
        elif type(inner) is RunSQL:
            parts.append(str(inner.sql))
            parts.append(str(inner.reverse_sql))
        elif classify(inner) == OPAQUE and type(inner) is not SeparateDatabaseAndState:
            # Custom operation: its class may hard-code what it touches.
            for cls in type(inner).__mro__:
                if cls.__module__.startswith("django.") or cls is object:
                    continue
                text, unknown = _callable_source(cls, migration_path)
                parts.append(text)
                match_all |= unknown
    return "\n".join(parts), match_all


_WORD = re.compile(r"[^\W_]+")


class OpaqueIndex:
    """
    Opaque operations with an inverted word index over their texts, so that
    "which opaque ops in this window mention one of these names" stays cheap
    when windows are wide (independent apps are unordered relative to each
    other, so their migrations fall into each other's windows).
    """

    def __init__(
        self,
        history: History,
        ops: list[OpaqueOp],
        texts: TextBuilder | None = None,
    ) -> None:
        self.history = history
        self.ops = ops
        self.texts = texts if texts is not None else TextBuilder()
        self.word_masks: dict[str, int] = {}
        self.match_all = 0
        for i, o in enumerate(ops):
            bit = 1 << i
            text = self.texts.text(o)
            if text.match_all:
                self.match_all |= bit
                continue
            for word in set(_WORD.findall(text.text)):
                self.word_masks[word] = self.word_masks.get(word, 0) | bit
        self._name_masks: dict[str, int] = {}

    def text(self, opaque: OpaqueOp) -> Text:
        return self.texts.text(opaque)

    def _candidates(self, name: str) -> int:
        mask = self._name_masks.get(name)
        if mask is None:
            words = _WORD.findall(name)
            if not words:
                mask = 0
            else:
                mask = -1
                for word in words:
                    mask &= self.word_masks.get(word, 0)
            self._name_masks[name] = mask
        return mask | self.match_all

    def _outside(self, start: Pos, end: Pos | None) -> int:
        outside = self.history.ancestors[start[0]]
        if end is not None:
            outside |= self.history.descendants[end[0]]
        return outside

    def contains(
        self, o: OpaqueOp, start: Pos, end: Pos | None, known_from: Pos
    ) -> bool:
        """
        Whether ``o`` may run while an entity created at ``start`` and ended
        at ``end`` exists, and may know about it. ``end=None`` means never
        ended.

        Project migrations unordered relative to either bound count as
        inside, since missing dependencies between project apps are common.
        A migration from an installed package can only act on the entity if
        it comes after the entity's creation (``known_from``) in the graph.
        """
        mig, idx = o.pos
        if self._outside(start, end) >> mig & 1:
            return False
        if mig == start[0] and idx < start[1]:
            return False
        if end is not None and mig == end[0] and idx > end[1]:
            return False
        if not o.rec.migration.editable:
            if mig == known_from[0]:
                return idx > known_from[1]
            return bool(self.history.ancestors[mig] >> known_from[0] & 1)
        return True

    def mentioning(
        self, names: Iterable[str], start: Pos, end: Pos | None, known_from: Pos
    ) -> list[tuple[OpaqueOp, str]]:
        """Opaque ops in the window mentioning any name, in plan order."""
        found: dict[int, str] = {}
        for name in sorted(names):
            mask = self._candidates(name)
            while mask:
                low = mask & -mask
                i = low.bit_length() - 1
                mask ^= low
                if i in found:
                    continue
                o = self.ops[i]
                if self.contains(o, start, end, known_from) and self.text(o).mentions(
                    name
                ):
                    found[i] = name
        return [(self.ops[i], found[i]) for i in sorted(found)]
