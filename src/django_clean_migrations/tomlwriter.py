"""
A small TOML writer for plan files (read back with the standard library's
tomllib).

Plan files have a fixed shape (tables, arrays of tables, strings, integers,
floats, booleans and arrays of those), so this writes just that, laid out
for reading top-down, rather than adding a dependency: keys keep their
order, None values are left out (TOML has no null), and long multi-line
strings (diffs) are written as multi-line literal strings when their
content allows, so they read as they are.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def dumps(data: Mapping[str, Any]) -> str:
    lines: list[str] = []
    _table(lines, (), data)
    return "\n".join(lines).strip("\n") + "\n"


def _table(lines: list[str], path: tuple[str, ...], data: Mapping[str, Any]) -> None:
    tables: list[tuple[str, Mapping[str, Any]]] = []
    arrays: list[tuple[str, Sequence[Mapping[str, Any]]]] = []
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, Mapping):
            tables.append((key, value))
        elif _is_table_array(value):
            arrays.append((key, value))
        else:
            lines.append(f"{_key(key)} = {_value(value)}")
    for key, table in tables:
        sub = (*path, key)
        lines.append("")
        lines.append(f"[{'.'.join(_key(k) for k in sub)}]")
        _table(lines, sub, table)
    for key, items in arrays:
        sub = (*path, key)
        for item in items:
            lines.append("")
            lines.append(f"[[{'.'.join(_key(k) for k in sub)}]]")
            _table(lines, sub, item)


def _is_table_array(value: Any) -> bool:
    """A list of tables is written as an array of tables ([[name]]); a tuple
    of tables as an inline array, for short records."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(v, Mapping) for v in value)
    )


def _key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _basic(key)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"can't write {value!r} to TOML")
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list | tuple):
        items = [v for v in value if v is not None]
        if any(isinstance(v, Mapping) for v in items):
            return "[" + ", ".join(_inline(v) for v in items) + "]"
        return "[" + ", ".join(_value(v) for v in items) + "]"
    raise TypeError(f"can't write {type(value).__name__} to TOML")


def _inline(value: Any) -> str:
    if isinstance(value, Mapping):
        return (
            "{ "
            + ", ".join(
                f"{_key(k)} = {_inline(v)}" for k, v in value.items() if v is not None
            )
            + " }"
        )
    return _value(value)


def _string(value: str) -> str:
    if "\n" not in value:
        return _basic(value)
    # Multi-line: literal (as is) when possible, else escaped.
    if (
        "'''" not in value
        and "\r" not in value
        and not _CONTROL.search(value)
        and not value.endswith("'")
    ):
        # A newline right after the opening delimiter is trimmed by TOML.
        return "'''\n" + value + "'''"
    return '"""\n' + _escape(value, multiline=True) + '"""'


def _basic(value: str) -> str:
    return '"' + _escape(value, multiline=False) + '"'


def _escape(value: str, *, multiline: bool) -> str:
    out = []
    for char in value:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n" and multiline:
            out.append("\n")
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            out.append("\\r")
        elif _CONTROL.match(char):
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    return "".join(out)
