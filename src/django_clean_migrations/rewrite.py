"""Source edits with tokenize-rt: only touched containers change."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

from django.db.migrations import operations as m
from django.db.migrations.operations.base import Operation
from django.db.migrations.state import ModelState
from django.db.migrations.writer import OperationWriter
from tokenize_rt import UNIMPORTANT_WS, Token  # type: ignore[import-untyped]

from django_clean_migrations.history import OpRecord, SourceFile
from django_clean_migrations.locate import (
    Entry,
    check_sub_elements,
    locate_create_model,
    locate_together_op,
    normalize_together,
)
from django_clean_migrations.tokens import Container, Segment

_NEWLINES = ("NL", "NEWLINE")


class RewriteError(Exception):
    pass


@dataclass(eq=False)
class OpEdit:
    rec: OpRecord
    remove: bool = False
    fields: set[str] = field(default_factory=set)
    indexes: set[int] = field(default_factory=set)
    constraints: set[int] = field(default_factory=set)
    together: dict[str, set[Entry]] = field(default_factory=dict)
    entries: set[Entry] = field(default_factory=set)
    replacement: Operation | None = None
    """Regenerate the operation's source from this object (folds)."""

    @property
    def empty(self) -> bool:
        return not (
            self.remove
            or self.replacement is not None
            or self.fields
            or self.indexes
            or self.constraints
            or any(self.together.values())
            or self.entries
        )


def _filter_together(value: Any, removed: set[Entry]) -> Any:
    remaining = [e for e in normalize_together(value) if e not in removed]
    if not remaining:
        return None
    if isinstance(value, (set, frozenset)):
        return set(remaining)
    if all(isinstance(v, str) for v in value):
        return value
    container = list if isinstance(value, list) else tuple
    inner = list if isinstance(next(iter(value)), list) else tuple
    return container(inner(e) for e in remaining)


def edited_operation(edit: OpEdit) -> Operation | None:
    """The operation the rewritten source will construct (None if removed)."""
    op = edit.rec.op
    if edit.remove:
        return None
    if edit.replacement is not None:
        return edit.replacement
    if edit.empty:
        return op
    if type(op) is m.CreateModel:
        options = dict(op.options)
        for key, removed_idx in (
            ("indexes", edit.indexes),
            ("constraints", edit.constraints),
        ):
            if removed_idx:
                options[key] = [
                    x for i, x in enumerate(options[key]) if i not in removed_idx
                ]
                if not options[key]:
                    del options[key]
        for option, removed in edit.together.items():
            if removed:
                value = _filter_together(options[option], removed)
                if value is None:
                    del options[option]
                else:
                    options[option] = value
        return m.CreateModel(
            name=op.name,
            fields=[(n, f) for n, f in op.fields if n not in edit.fields],
            options=options,
            bases=op.bases,
            managers=op.managers,
        )
    if type(op) is m.AlterUniqueTogether or type(op) is m.AlterIndexTogether:
        value = {
            e for e in normalize_together(op.option_value) if e not in edit.entries
        }
        return type(op)(op.name, value)
    raise RewriteError(f"cannot edit {type(op).__name__}")


class FileEdit:
    def __init__(self, source: SourceFile) -> None:
        self.source = source
        self.tokens: list[Token] = source.tokens
        self.deleted: set[int] = set()
        self.replace: dict[int, str] = {}
        self.insert_after: dict[int, str] = {}
        self.imports_added: set[str] = set()

    def container(self, node: ast.expr) -> Container:
        container = self.source.container(node)
        if container is None:
            raise RewriteError(f"cannot edit container at line {node.lineno}")
        return container

    def remove(self, node: ast.expr, indexes: set[int]) -> None:
        if not indexes:
            return
        self.remove_from(self.container(node), indexes)

    def remove_from(self, c: Container, remove: set[int]) -> None:
        toks = self.tokens
        segs = c.segments
        kept = [k for k in range(len(segs)) if k not in remove]
        if not kept:
            self.deleted.update(range(c.opener + 1, c.closer))
            if c.kind == "set":
                self.replace[c.opener] = "set()"
                self.deleted.add(c.closer)
            return
        single_line = toks[c.opener].line == toks[c.closer].line
        last_kept = kept[-1]
        for k in sorted(remove):
            seg = segs[k]
            if k > last_kept:
                continue
            assert seg.comma is not None
            lines = None if single_line else self._line_range(seg, c.opener)
            if lines is not None:
                self.deleted.update(range(*lines))
                continue
            self.deleted.update(range(seg.start, seg.comma + 1))
            if toks[seg.comma + 1].name == UNIMPORTANT_WS:
                self.deleted.add(seg.comma + 1)
            elif (
                toks[seg.comma + 1].name in _NEWLINES
                and toks[seg.start - 1].name == UNIMPORTANT_WS
                and toks[seg.start - 2].name not in _NEWLINES
            ):
                self.deleted.add(seg.start - 1)
        trailing = [k for k in sorted(remove) if k > last_kept]
        if trailing:
            if single_line:
                self.deleted.update(range(segs[last_kept].end + 1, c.closer))
            else:
                for k in trailing:
                    seg = segs[k]
                    lines = self._line_range(seg, c.opener)
                    if lines is not None:
                        self.deleted.update(range(*lines))
                        continue
                    tail = seg.comma if seg.comma is not None else seg.end
                    start = seg.start
                    while toks[start - 1].name in (UNIMPORTANT_WS, "NL"):
                        start -= 1
                    if toks[start - 1].name == "COMMENT":
                        start += 1  # keep the comment line, line break included
                    self.deleted.update(range(start, tail + 1))
        if c.kind == "tuple" and len(kept) == 1:
            seg = segs[kept[0]]
            if seg.comma is None or seg.comma in self.deleted:
                self.insert_after[seg.end] = ","

    def _line_range(self, seg: Segment, opener: int) -> tuple[int, int] | None:
        """
        Token range covering the whole line(s) of an element written on lines
        of its own, plus comment lines directly attached above it.
        """
        toks = self.tokens
        j = seg.start - 1
        while toks[j].name == UNIMPORTANT_WS:
            j -= 1
        if toks[j].name not in _NEWLINES:
            return None
        start = j + 1
        j = (seg.comma if seg.comma is not None else seg.end) + 1
        while toks[j].name in (UNIMPORTANT_WS, "COMMENT"):
            j += 1
        if toks[j].name not in _NEWLINES:
            return None
        end = j + 1
        while True:
            p = start - 2
            if p <= opener or toks[p].name != "COMMENT":
                break
            q = p - 1
            if toks[q].name == UNIMPORTANT_WS:
                q -= 1
            if q <= opener or toks[q].name not in _NEWLINES:
                break
            start = q + 1
        return start, end

    def regenerate(self, node: ast.Call, op: Operation) -> None:
        """Replace an operation call with source written by Django's writer."""
        text, imports = serialize_operation(op)
        missing = _missing_imports(self.source, imports)
        if isinstance(missing, str):
            raise RewriteError(missing)
        start = self.source.token_index.get((node.lineno, node.col_offset))
        assert node.end_lineno is not None and node.end_col_offset is not None
        end = self.source.token_index.get((node.end_lineno, node.end_col_offset - 1))
        if start is None or end is None:
            raise RewriteError(f"cannot find the operation at line {node.lineno}")
        newline = "\r\n" if "\r\n" in self.source.text else "\n"
        line_start = self.source.char_offset(node.lineno, 0)
        column = self.source.char_offset(node.lineno, node.col_offset) - line_start
        lines = text.split("\n")
        text = newline.join([lines[0], *(" " * column + line for line in lines[1:])])
        self.deleted.update(range(start + 1, end + 1))
        self.replace[start] = text
        if missing:
            anchor = _import_anchor(self.source)
            if anchor is None:
                raise RewriteError("no top-level imports to add imports after")
            # Several regenerated operations in a file may need the same one.
            fresh = [line for line in missing if line not in self.imports_added]
            self.imports_added.update(fresh)
            added = "".join(line + newline for line in fresh)
            self.insert_after[anchor] = self.insert_after.get(anchor, "") + added

    def render(self) -> str:
        out: list[str] = []
        for i, tok in enumerate(self.tokens):
            if i in self.deleted:
                continue
            out.append(self.replace.get(i, tok.src))
            if i in self.insert_after:
                out.append(self.insert_after[i])
        return "".join(out)


def rewrite_source(source: SourceFile, edits: list[OpEdit]) -> str:
    fe = FileEdit(source)
    removed_ops: set[int] = set()
    ops_container: ast.expr | None = None
    for edit in edits:
        rec = edit.rec
        info = rec.migration
        if info.ops_container is None:
            raise RewriteError(f"operations of {info.label} are not editable")
        ops_container = info.ops_container
        if edit.remove:
            removed_ops.add(rec.index)
            continue
        if edit.empty:
            continue
        if edit.replacement is not None:
            node = rec.node
            assert node is not None  # located along with the operations list
            fe.regenerate(node, edit.replacement)
            continue
        if type(rec.op) is m.CreateModel:
            loc = locate_create_model(rec)
            if isinstance(loc, str):
                raise RewriteError(loc)
            fe.remove(loc.fields, {loc.field_index[n] for n in edit.fields})
            dropped_keys: set[int] = set()
            for key, removed_idx in (
                ("indexes", edit.indexes),
                ("constraints", edit.constraints),
            ):
                if removed_idx and len(removed_idx) == len(loc.lists[key].elts):
                    dropped_keys.add(loc.option_index[key])
                elif removed_idx:
                    fe.remove(loc.lists[key], removed_idx)
            for option, removed in edit.together.items():
                if not removed:
                    continue
                tloc = loc.together[option]
                if len(tloc.entries) == len(removed):
                    dropped_keys.add(loc.option_index[option])
                else:
                    assert tloc.node is not None and not tloc.flat
                    fe.remove(tloc.node, {tloc.entries[e] for e in removed})
            if dropped_keys:
                assert loc.options is not None
                fe.remove(loc.options, dropped_keys)
        else:
            tloc2 = locate_together_op(rec)
            if isinstance(tloc2, str):
                raise RewriteError(tloc2)
            assert tloc2.node is not None
            if tloc2.flat:
                fe.remove(
                    tloc2.node, set(range(len(fe.container(tloc2.node).segments)))
                )
            else:
                fe.remove(tloc2.node, {tloc2.entries[e] for e in edit.entries})
    if removed_ops:
        assert ops_container is not None
        fe.remove(ops_container, removed_ops)
    return fe.render()


def serialize_operation(op: Operation) -> tuple[str, set[str]]:
    """Source for ``op`` as makemigrations writes it, minus the trailing comma."""
    text, imports = OperationWriter(op, indentation=0).serialize()
    return text.rstrip().removesuffix(","), {
        *imports,
        "from django.db import migrations",
    }


_IMPORT_RE = re.compile(r"^(?:from ([\w.]+) import ([\w, ]+)|import ([\w.]+))$")


def _bindings(tree: ast.Module) -> tuple[dict[str, tuple[str, ...]], set[str]]:
    """Top-level names and what binds them; and the modules imported whole."""
    bound: dict[str, tuple[str, ...]] = {}
    modules: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    bound[alias.asname] = ("import as", alias.name)
                else:
                    bound[alias.name.split(".")[0]] = (
                        "import",
                        alias.name.split(".")[0],
                    )
                    modules.add(alias.name)
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                bound[alias.asname or alias.name] = (
                    "from",
                    "." * stmt.level + (stmt.module or ""),
                    alias.name,
                )
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.setdefault(stmt.name, ("other",))
        else:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    bound.setdefault(node.id, ("other",))
    return bound, modules


def _missing_imports(source: SourceFile, imports: set[str]) -> list[str] | str:
    """
    Import lines to add so ``imports`` (from the migration writer) hold in
    ``source``, or a reason they can't: an unimportable module path, or a
    name the file already binds to something else.
    """
    tree = source.tree
    bound, modules = _bindings(tree)
    missing: list[str] = []
    for line in sorted(imports):
        match = _IMPORT_RE.match(line.strip())
        if match is None:
            return f"unsupported import {line!r}"
        module = match.group(1) or match.group(3)
        if not all(part.isidentifier() for part in module.split(".")):
            return f"{module!r} is not an importable module path"
        if match.group(1):
            needed = False
            for name in (n.strip() for n in match.group(2).split(",")):
                want = ("from", module, name)
                have = bound.get(name)
                if have is None:
                    needed = True
                elif have != want:
                    return f"{name!r} is already bound to something else"
            if needed:
                missing.append(line)
        else:
            top = module.split(".")[0]
            have = bound.get(top)
            if have is not None and have != ("import", top):
                return f"{top!r} is already bound to something else"
            if module not in modules:
                missing.append(line)
    return missing


def _import_anchor(source: SourceFile) -> int | None:
    """Token after which to add imports: the end of the last top-level one."""
    imports = [
        stmt
        for stmt in source.tree.body
        if isinstance(stmt, (ast.Import, ast.ImportFrom))
    ]
    if not imports:
        return None
    line = imports[-1].end_lineno
    return [
        i
        for i, tok in enumerate(source.tokens)
        if tok.name == "NEWLINE" and tok.line == line
    ][0]


def regeneration_problem(source: SourceFile | None, op: Operation) -> str | None:
    """Why ``op`` can't be written into ``source`` by regeneration, if so."""
    if source is None:
        return "the source is not readable"
    try:
        text, imports = serialize_operation(op)
    except Exception as exc:
        return f"the operation can't be serialized ({exc})"
    missing = _missing_imports(source, imports)
    if isinstance(missing, str):
        return missing
    try:
        namespace: dict[str, Any] = {}
        for line in sorted(imports):
            exec(line, namespace)
        rebuilt = eval(text, namespace)
    except Exception as exc:
        return f"the regenerated source doesn't load ({exc!r})"
    if not same_operation(op, rebuilt):
        return "the regenerated source wouldn't reproduce the operation"
    return None


def same_operation(a: Operation, b: Operation) -> bool:
    """Whether two operations have the same effect on the project state."""
    from django_clean_migrations.verify import model_states_equal, same_value

    if type(a) is not type(b):
        return False
    if isinstance(a, m.CreateModel):
        assert isinstance(b, m.CreateModel)

        def state(op: m.CreateModel) -> ModelState:
            return ModelState(
                "app",
                op.name,
                list(op.fields),
                dict(op.options),
                tuple(op.bases or ()),
                list(op.managers or ()),
            )

        return model_states_equal(state(a), state(b))
    return same_value(a.deconstruct()[1:], b.deconstruct()[1:])


def _kept(old: list[Any], new: list[Any]) -> set[int] | None:
    """Indexes of ``old`` missing from ``new`` if new is a subsequence of old
    (by identity), else None."""
    removed: set[int] = set()
    j = 0
    for i, item in enumerate(old):
        if j < len(new) and new[j] is item:
            j += 1
        else:
            removed.add(i)
    return removed if j == len(new) else None


def derive_op_edit(rec: OpRecord, new: Operation) -> OpEdit | None:
    """
    The token-level edit turning ``rec``'s operation into ``new``, if new only
    lacks parts of it; None if it must be regenerated.
    """
    old = rec.op
    if type(old) is not type(new):
        return None
    if type(old) is m.CreateModel:
        assert isinstance(new, m.CreateModel)
        if (
            new.name != old.name
            or tuple(new.bases or ()) != tuple(old.bases or ())
            or list(new.managers or ()) != list(old.managers or ())
        ):
            return None
        removed_fields = _kept([f for _, f in old.fields], [f for _, f in new.fields])
        if removed_fields is None:
            return None
        kept_names = [
            n for i, (n, _) in enumerate(old.fields) if i not in removed_fields
        ]
        if kept_names != [n for n, _ in new.fields]:
            return None
        edit = OpEdit(rec, fields={old.fields[i][0] for i in removed_fields})
        if not set(new.options) <= set(old.options):
            return None
        for key, value in old.options.items():
            if key in ("indexes", "constraints"):
                gone = _kept(list(value), list(new.options.get(key, [])))
                if gone is None:
                    return None
                if key == "indexes":
                    edit.indexes = gone
                else:
                    edit.constraints = gone
            elif key in ("unique_together", "index_together"):
                before = set(normalize_together(value))
                after = set(normalize_together(new.options.get(key)))
                if not after <= before:
                    return None
                if before - after:
                    edit.together[key] = before - after
            elif key not in new.options or new.options[key] is not value:
                return None
        return _locatable(edit)
    if type(old) in (m.AlterUniqueTogether, m.AlterIndexTogether):
        assert isinstance(old, (m.AlterUniqueTogether, m.AlterIndexTogether))
        assert isinstance(new, (m.AlterUniqueTogether, m.AlterIndexTogether))
        if new.name != old.name:
            return None
        before = set(normalize_together(old.option_value))
        after = set(normalize_together(new.option_value))
        if not after <= before:
            return None
        return _locatable(OpEdit(rec, entries=before - after))
    return None


def _locatable(edit: OpEdit) -> OpEdit | None:
    """The edit, if the parts it removes can be found in the source; else
    None, to regenerate the operation."""
    subs: list[tuple[Any, ...]] = [
        *(("field", name) for name in edit.fields),
        *(("index", i) for i in edit.indexes),
        *(("constraint", i) for i in edit.constraints),
        *(
            ("together", option, entry)
            for option, entries in edit.together.items()
            for entry in entries
        ),
        *(("entry", entry) for entry in edit.entries),
    ]
    return edit if check_sub_elements(edit.rec, subs) is None else None


def unchanged(old: Operation, new: Operation) -> bool:
    """Whether ``new`` would be written exactly as ``old`` would."""
    if type(old) is not type(new):
        return False
    try:
        return serialize_operation(old) == serialize_operation(new)
    except Exception:
        return False
