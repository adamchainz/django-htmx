"""
Locate sub-elements (CreateModel fields/options entries, together entries) in
migration source. Any mismatch between source and loaded operation means the
sub-element is not editable.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db.migrations.operations import (
    AlterIndexTogether,
    AlterUniqueTogether,
    CreateModel,
)

from django_clean_migrations.history import OpRecord, SourceFile

TOGETHER_OPTIONS = ("unique_together", "index_together")
LIST_OPTIONS = {"index": "indexes", "constraint": "constraints"}

Entry = tuple[str, ...]


def normalize_together(value: Any) -> list[Entry]:
    if not value:
        return []
    items = list(value)
    if all(isinstance(i, str) for i in items):
        return [tuple(items)]
    return [tuple(i) for i in items]


@dataclass(frozen=True)
class TogetherLoc:
    node: ast.expr | None
    """Container holding the entries (for a flat value, the flat tuple itself)."""
    flat: bool
    entries: dict[Entry, int]


@dataclass(frozen=True)
class CreateModelLoc:
    fields: ast.List | ast.Tuple
    field_index: dict[str, int]
    options: ast.Dict | None
    option_index: dict[str, int]
    lists: dict[str, ast.List | ast.Tuple]
    together: dict[str, TogetherLoc]


class NotLocatable(Exception):
    pass


def _arg(call: ast.Call, position: int, name: str) -> ast.expr | None:
    if any(isinstance(a, ast.Starred) for a in call.args) or any(
        k.arg is None for k in call.keywords
    ):
        raise NotLocatable("call uses * or ** arguments")
    if len(call.args) > position:
        return call.args[position]
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _str_const(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _check_container(source: SourceFile, node: ast.expr) -> None:
    if source.container(node) is None:
        raise NotLocatable("unexpected source layout")


def _str_tuple(node: ast.expr) -> Entry | None:
    if not isinstance(node, (ast.Tuple, ast.List)):
        return None
    names = [_str_const(e) for e in node.elts]
    if any(n is None for n in names):
        return None
    return tuple(n for n in names if n is not None)


def _locate_together_value(
    source: SourceFile, node: ast.expr | None, value: Any
) -> TogetherLoc:
    expected = normalize_together(value)
    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        if expected:
            raise NotLocatable("together value not found")
        return TogetherLoc(None, False, {})
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "set"
        and not node.args
        and not node.keywords
    ):
        if expected:
            raise NotLocatable("together value mismatch")
        return TogetherLoc(None, False, {})
    if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        raise NotLocatable("together value is not a literal")
    if node.elts and all(_str_const(e) is not None for e in node.elts):
        flat = _str_tuple(node) if not isinstance(node, ast.Set) else None
        if flat is None or expected != [flat]:
            raise NotLocatable("together value mismatch")
        _check_container(source, node)
        return TogetherLoc(node, True, {flat: 0})
    entries: dict[Entry, int] = {}
    for i, elt in enumerate(node.elts):
        entry = _str_tuple(elt)
        if entry is None or entry in entries:
            raise NotLocatable("together entry is not a literal")
        entries[entry] = i
    if set(entries) != set(expected) or len(entries) != len(set(expected)):
        raise NotLocatable("together value mismatch")
    _check_container(source, node)
    return TogetherLoc(node, False, entries)


def _cached(rec: OpRecord, key: str, compute: Any) -> Any:
    if key not in rec._sub_cache:
        try:
            rec._sub_cache[key] = compute()
        except NotLocatable as exc:
            rec._sub_cache[key] = f"{exc} in {rec.describe()}"
    return rec._sub_cache[key]


def locate_create_model(rec: OpRecord) -> CreateModelLoc | str:
    return _cached(rec, "create_model", lambda: _locate_create_model(rec))  # type: ignore[no-any-return]


def _locate_create_model(rec: OpRecord) -> CreateModelLoc:
    op = rec.op
    assert isinstance(op, CreateModel)
    call = rec.node
    source = rec.migration.source
    if call is None or source is None:
        raise NotLocatable("operation not located")
    fields_node = _arg(call, 1, "fields")
    if not isinstance(fields_node, (ast.List, ast.Tuple)):
        raise NotLocatable("fields is not a literal")
    names: list[str] = []
    for elt in fields_node.elts:
        if not isinstance(elt, ast.Tuple) or len(elt.elts) != 2:
            raise NotLocatable("fields entry is not a 2-tuple")
        name = _str_const(elt.elts[0])
        if name is None:
            raise NotLocatable("field name is not a string literal")
        names.append(name)
    if names != [name for name, _ in op.fields]:
        raise NotLocatable("fields do not match")
    _check_container(source, fields_node)

    options_node = _arg(call, 2, "options")
    option_index: dict[str, int] = {}
    lists: dict[str, ast.List | ast.Tuple] = {}
    together: dict[str, TogetherLoc] = {}
    options_dict: ast.Dict | None = None
    if options_node is None or (
        isinstance(options_node, ast.Constant) and options_node.value is None
    ):
        if op.options:
            raise NotLocatable("options not found")
    else:
        if not isinstance(options_node, ast.Dict):
            raise NotLocatable("options is not a dict literal")
        options_dict = options_node
        for i, key_node in enumerate(options_node.keys):
            key_str = _str_const(key_node) if key_node is not None else None
            if key_str is None or key_str in option_index:
                raise NotLocatable("options keys are not string literals")
            option_index[key_str] = i
        if set(option_index) != set(op.options):
            raise NotLocatable("options do not match")
        _check_container(source, options_node)
        for key, i in option_index.items():
            value_node = options_node.values[i]
            if key in ("indexes", "constraints"):
                if not isinstance(value_node, (ast.List, ast.Tuple)) or len(
                    value_node.elts
                ) != len(op.options[key]):
                    raise NotLocatable(f"{key} option is not a matching literal")
                _check_container(source, value_node)
                lists[key] = value_node
            elif key in TOGETHER_OPTIONS:
                together[key] = _locate_together_value(
                    source, value_node, op.options[key]
                )
    return CreateModelLoc(
        fields=fields_node,
        field_index={n: i for i, n in enumerate(names)},
        options=options_dict,
        option_index=option_index,
        lists=lists,
        together=together,
    )


def locate_together_op(rec: OpRecord) -> TogetherLoc | str:
    return _cached(rec, "together", lambda: _locate_together_op(rec))  # type: ignore[no-any-return]


def _locate_together_op(rec: OpRecord) -> TogetherLoc:
    op = rec.op
    assert isinstance(op, (AlterUniqueTogether, AlterIndexTogether))
    call = rec.node
    source = rec.migration.source
    if call is None or source is None:
        raise NotLocatable("operation not located")
    option_name = op.option_name
    assert option_name is not None
    node = _arg(call, 1, option_name)
    return _locate_together_value(source, node, op.option_value)


def check_sub_elements(rec: OpRecord, subs: Iterable[tuple[Any, ...]]) -> str | None:
    """Return a reason if any of the given sub-element keys is not editable."""
    reason = rec.not_editable_reason()
    if reason is not None:
        return reason
    for sub in subs:
        kind = sub[0]
        if kind in ("field", "index", "constraint", "together"):
            loc = locate_create_model(rec)
            if isinstance(loc, str):
                return loc
            if kind == "field" and sub[1] not in loc.field_index:
                return f"field {sub[1]!r} not found in {rec.describe()}"
            if kind in LIST_OPTIONS and LIST_OPTIONS[kind] not in loc.lists:
                return f"{kind} not found in {rec.describe()}"
            if kind == "together":
                tloc = loc.together.get(sub[1])
                if tloc is None or sub[2] not in tloc.entries:
                    return f"{sub[1]} entry not found in {rec.describe()}"
        elif kind == "entry":
            tloc2 = locate_together_op(rec)
            if isinstance(tloc2, str):
                return tloc2
            if sub[1] not in tloc2.entries:
                return f"together entry not found in {rec.describe()}"
    return None
