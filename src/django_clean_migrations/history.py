from __future__ import annotations

import ast
import copy
import dataclasses
import importlib.util
import io
import re
import sys
import tokenize
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from django.db.migrations.graph import MigrationGraph
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.migration import Migration
from django.db.migrations.operations.base import Operation
from tokenize_rt import Token, src_to_tokens  # type: ignore[import-untyped]

from django_clean_migrations import tokens as tk

Key = tuple[str, str]


class HistoryError(Exception):
    pass


_LINE_BREAK = re.compile(r"\r\n|\r|\n")


class SourceFile:
    def __init__(self, path: Path, text: str, encoding: str = "utf-8") -> None:
        self.path = path
        self.text = text
        self.encoding = encoding
        """As Python reads the file (a coding cookie, a BOM, else UTF-8)."""

    @cached_property
    def tree(self) -> ast.Module:
        # The file was imported as a module, so it parses.
        return ast.parse(self.text)

    @cached_property
    def tokens(self) -> list[Token]:
        return list(src_to_tokens(self.text))

    @cached_property
    def token_index(self) -> dict[tuple[int, int], int]:
        return {
            (t.line, t.utf8_byte_offset): i
            for i, t in enumerate(self.tokens)
            if t.line is not None and t.utf8_byte_offset is not None
        }

    @cached_property
    def _line_starts(self) -> list[int]:
        # Only the line breaks ast/tokenize count; str.splitlines() also
        # splits on form feeds, U+2028 and friends.
        starts = [0, 0]
        starts.extend(m.end() for m in _LINE_BREAK.finditer(self.text))
        if starts[-1] != len(self.text):
            starts.append(len(self.text))
        return starts

    def char_offset(self, line: int, utf8_col: int) -> int:
        start = self._line_starts[line]
        line_text = self.text[start : self._line_starts[line + 1]]
        return start + len(line_text.encode()[:utf8_col].decode())

    def node_span(self, node: ast.expr | ast.stmt) -> tuple[int, int]:
        assert node.end_lineno is not None and node.end_col_offset is not None
        return (
            self.char_offset(node.lineno, node.col_offset),
            self.char_offset(node.end_lineno, node.end_col_offset),
        )

    def container(self, node: ast.expr) -> tk.Container | None:
        """
        Token-level view of a literal container node, or None if its tokens
        don't line up with the AST (in which case it must not be edited).
        """
        cache: dict[int, tk.Container | None] = self.__dict__.setdefault(
            "_containers", {}
        )
        if id(node) not in cache:
            cache[id(node)] = tk.container_for_node(self.tokens, self.token_index, node)
        return cache[id(node)]


def read_python(path: Path) -> tuple[str, str]:
    """The exact text of a Python file (line endings kept), decoded as
    Python would (coding cookie or BOM), and that encoding."""
    data = path.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding), encoding


def _read_source(path: Path) -> SourceFile | None:
    try:
        text, encoding = read_python(path)
    except (OSError, UnicodeDecodeError, SyntaxError, LookupError):
        return None
    return SourceFile(path, text, encoding)


@dataclass(eq=False)
class MigrationInfo:
    key: Key
    migration: Migration
    index: int
    path: Path | None
    editable: bool
    source: SourceFile | None
    op_nodes: list[ast.Call] | None = None
    ops_container: ast.List | ast.Tuple | None = None
    unlocatable_reason: str | None = None
    original: MigrationInfo | None = None
    """For a derived history (later fold rounds): the migration as on disk."""

    @property
    def on_disk(self) -> MigrationInfo:
        return self.original if self.original is not None else self

    @property
    def app_label(self) -> str:
        return self.key[0]

    @property
    def name(self) -> str:
        return self.key[1]

    @property
    def label(self) -> str:
        return f"{self.key[0]}.{self.key[1]}"

    @property
    def locatable(self) -> bool:
        return self.op_nodes is not None


@dataclass(eq=False)
class OpRecord:
    migration: MigrationInfo
    index: int
    op: Operation
    seq: int
    _sub_cache: dict[str, Any] = field(default_factory=dict, repr=False)
    origin: int | None = None
    """Index of the operation this one derives from in the file on disk."""
    derived: bool = False
    """The operation object differs from the one loaded from disk (an
    earlier round edited it); its parts are edited by regenerating it."""

    @property
    def original_index(self) -> int:
        return self.index if self.origin is None else self.origin

    @property
    def node(self) -> ast.Call | None:
        if self.migration.op_nodes is None:
            return None
        return self.migration.op_nodes[self.index]

    @property
    def name(self) -> str:
        return type(self.op).__name__

    @property
    def line(self) -> int | None:
        node = self.node
        return node.lineno if node is not None else None

    def describe(self) -> str:
        return f"{self.name} in {self.migration.label}"

    def not_editable_reason(self) -> str | None:
        mig = self.migration.on_disk
        if not mig.editable:
            return f"{mig.label} is not editable (installed package)"
        if not mig.locatable:
            return (
                f"operations list in {mig.label} could not be parsed"
                f" ({mig.unlocatable_reason})"
            )
        return None


@dataclass(eq=False)
class History:
    loader: MigrationLoader
    plan: list[Key]
    migrations: list[MigrationInfo]
    by_key: dict[Key, MigrationInfo]
    ops: list[OpRecord]
    warnings: list[str]

    @cached_property
    def ancestors(self) -> list[int]:
        """Bitmask (over plan indexes) of strict graph ancestors per migration."""
        graph = self.loader.graph
        result = [0] * len(self.plan)
        for i, key in enumerate(self.plan):
            mask = 0
            for parent in graph.node_map[key].parents:
                p = self.by_key[parent.key].index
                mask |= result[p] | (1 << p)
            result[i] = mask
        return result

    @cached_property
    def descendants(self) -> list[int]:
        graph = self.loader.graph
        result = [0] * len(self.plan)
        for i in range(len(self.plan) - 1, -1, -1):
            mask = 0
            for child in graph.node_map[self.plan[i]].children:
                c = self.by_key[child.key].index
                mask |= result[c] | (1 << c)
            result[i] = mask
        return result

    def ordered(self, a: int, b: int) -> bool:
        """Whether migrations a and b (plan indexes) are ordered in every plan."""
        return (
            a == b
            or bool(self.ancestors[a] >> b & 1)
            or bool(self.descendants[a] >> b & 1)
        )


def generate_plan(graph: MigrationGraph) -> list[Key]:
    """
    Same order as MigrationGraph._generate_plan(leaf_nodes(), at_end=True),
    i.e. what ``migrate`` and ``loader.project_state()`` use, without its
    quadratic list membership test.
    """
    plan: list[Key] = []
    seen: set[Key] = set()
    for leaf in graph.leaf_nodes():
        for key in graph.forwards_plan(leaf):
            if key not in seen:
                seen.add(key)
                plan.append(key)
    return plan


def migration_path(migration: Migration) -> Path | None:
    module = sys.modules.get(type(migration).__module__)
    filename = getattr(module, "__file__", None)
    if not filename or not filename.endswith(".py"):
        return None
    return Path(filename)


def is_editable_path(path: Path) -> bool:
    return not any(part in ("site-packages", "dist-packages") for part in path.parts)


def callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _find_operations(
    tree: ast.Module,
) -> tuple[ast.expr | None, str | None]:
    classes = [
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Migration"
    ]
    if len(classes) != 1:
        return None, "no unique top-level class Migration"
    values: list[ast.expr] = []
    for stmt in classes[0].body:
        if isinstance(stmt, ast.Assign):
            if any(
                isinstance(t, ast.Name) and t.id == "operations" for t in stmt.targets
            ):
                if len(stmt.targets) != 1:
                    return None, "chained assignment to operations"
                values.append(stmt.value)
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == "operations"
        ):
            if stmt.value is None:
                return None, "annotation without value"
            values.append(stmt.value)
        elif isinstance(stmt, ast.AugAssign) and (
            isinstance(stmt.target, ast.Name) and stmt.target.id == "operations"
        ):
            return None, "augmented assignment to operations"
    if len(values) != 1:
        return None, "no unique operations assignment"
    return values[0], None


def locate_operations(info: MigrationInfo) -> None:
    source = info.source
    if source is None:
        info.unlocatable_reason = "source not readable"
        return
    tree = source.tree
    value, reason = _find_operations(tree)
    if value is None:
        info.unlocatable_reason = reason
        return
    if not isinstance(value, (ast.List, ast.Tuple)):
        info.unlocatable_reason = "operations is not a list or tuple literal"
        return
    ops = info.migration.operations
    if any(isinstance(e, ast.Starred) for e in value.elts):
        info.unlocatable_reason = "operations contains starred expressions"
        return
    if len(value.elts) != len(ops):
        info.unlocatable_reason = "operations count does not match"
        return
    nodes: list[ast.Call] = []
    for elt, op in zip(value.elts, ops, strict=True):
        if not isinstance(elt, ast.Call) or callee_name(elt.func) != type(op).__name__:
            info.unlocatable_reason = f"cannot match {type(op).__name__} in source"
            return
        nodes.append(elt)
    if source.container(value) is None:
        info.unlocatable_reason = "unexpected token layout"
        return
    info.ops_container = value
    info.op_nodes = nodes


def load_history(loader: MigrationLoader | None = None) -> History:
    if loader is None:
        loader = MigrationLoader(None, ignore_no_migrations=True)
    graph = loader.graph
    plan = generate_plan(graph)
    warnings: list[str] = []
    in_graph = set(plan)
    skipped = sorted(k for k in loader.disk_migrations if k not in in_graph)
    if skipped:
        warnings.append(
            f"{len(skipped)} migration(s) replaced by squashed migrations were"
            " skipped: "
            + ", ".join(f"{a}.{n}" for a, n in skipped[:10])
            + (", ..." if len(skipped) > 10 else "")
        )
    migrations: list[MigrationInfo] = []
    by_key: dict[Key, MigrationInfo] = {}
    ops: list[OpRecord] = []
    for index, key in enumerate(plan):
        migration = graph.nodes[key]
        path = migration_path(migration)
        source = _read_source(path) if path is not None else None
        editable = path is not None and is_editable_path(path) and source is not None
        info = MigrationInfo(
            key=key,
            migration=migration,
            index=index,
            path=path,
            editable=editable,
            source=source,
        )
        locate_operations(info)
        migrations.append(info)
        by_key[key] = info
        for i, op in enumerate(migration.operations):
            ops.append(OpRecord(info, i, op, len(ops)))
    return History(loader, plan, migrations, by_key, ops, warnings)


def derive_history(
    original: History,
    operations: dict[int, list[Operation]],
    origins: dict[int, tuple[int, bool]],
) -> History:
    """
    A history whose migrations have the given operation lists (by plan
    index; others unchanged). ``origins`` maps id(op) of each listed
    operation to (index in the original migration, whether it was edited).
    Source locations and opaque-operation texts still come from disk.
    """
    migrations: list[MigrationInfo] = []
    by_key: dict[Key, MigrationInfo] = {}
    ops: list[OpRecord] = []
    for info in original.migrations:
        new_ops = operations.get(info.index)
        if new_ops is None:
            derived_info = info
            entries = [(op, i, False) for i, op in enumerate(info.migration.operations)]
        else:
            migration = copy.copy(info.migration)
            vars(migration)["operations"] = list(new_ops)
            entries = [(op, *origins[id(op)]) for op in new_ops]
            nodes = (
                [info.op_nodes[i] for _, i, _ in entries]
                if info.op_nodes is not None
                else None
            )
            derived_info = dataclasses.replace(
                info, migration=migration, op_nodes=nodes, original=info.on_disk
            )
        migrations.append(derived_info)
        by_key[derived_info.key] = derived_info
        for i, (op, origin, edited) in enumerate(entries):
            ops.append(
                OpRecord(derived_info, i, op, len(ops), origin=origin, derived=edited)
            )
    history = History(
        original.loader, original.plan, migrations, by_key, ops, original.warnings
    )
    history.__dict__["ancestors"] = original.ancestors
    history.__dict__["descendants"] = original.descendants
    return history


def invalidate_modules(module_names: Iterable[str], paths: Sequence[Path]) -> None:
    """Forget imported migration modules and their bytecode so they reload."""
    importlib.invalidate_caches()
    for name in module_names:
        sys.modules.pop(name, None)
    for path in paths:
        try:
            Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
        except (OSError, ValueError, NotImplementedError):
            pass
