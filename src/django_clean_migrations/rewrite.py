"""Source edits with tokenize-rt: only touched containers change."""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

from django.db.migrations import operations as m
from django.db.migrations.operations.base import Operation
from tokenize_rt import UNIMPORTANT_WS, Token  # type: ignore[import-untyped]

from django_clean_migrations.history import OpRecord, SourceFile
from django_clean_migrations.locate import (
    Entry,
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

    @property
    def empty(self) -> bool:
        return not (
            self.remove
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
                        while toks[start].name != "NL":
                            start += 1
                        start += 1
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
