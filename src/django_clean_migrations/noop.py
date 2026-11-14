"""
Detecting data migrations that do nothing on a fresh database.

While the original history runs on a scratch database, every statement a
RunPython/RunSQL operation executes, on every configured connection, is
recorded with its row count. The operation is a no-op there iff each one is
a read, transaction control, or an UPDATE/DELETE that affected no rows.
Anything else, or anything not recognised with confidence, counts as a
write. What it does outside the database is invisible: hence opt-in.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any

from django.db import connections

_COMMENTS = re.compile(r"(--[^\n]*\n?)|(/\*.*?\*/)", re.S)
_CALL = re.compile(r"([a-z_][a-z0-9_.]*)\s*\(", re.I)
_CONTROL = frozenset(
    {"SAVEPOINT", "RELEASE", "BEGIN", "COMMIT", "START", "END", "ROLLBACK"}
)
# Functions Django's ORM puts in SELECTs that can't change anything.
_PURE_FUNCTIONS = frozenset(
    {
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "coalesce",
        "cast",
        "upper",
        "lower",
        "exists",
        "length",
        "char_length",
        "substring",
        "substr",
        "concat",
        "abs",
        "round",
        "trim",
        "extract",
        "date_trunc",
        "django_date_trunc",
        "django_datetime_trunc",
        "django_datetime_cast_date",
        "django_format_dtdelta",
        "nullif",
        "greatest",
        "least",
        "in",
        "any",
        "array_agg",
        "string_agg",
        "group_concat",
        "lpad",
        "rpad",
        "replace",
        "strftime",
        "ifnull",
        "json_extract",
        "jsonb_extract_path",
        "row_number",
        "rank",
        "over",
        "values",
        "now",
        "current_timestamp",
        "and",
        "or",
        "not",
        "on",
        "as",
        "from",
        "where",
        "select",
        "case",
        "when",
        "then",
        "else",
        "join",
        "by",
        "having",
        "is",
        "between",
        "like",
        "ilike",
        "distinct",
        "array",
        "row",
        "lateral",
        "union",
        "all",
        "jsonb_build_object",
        "json_object",
        "to_char",
        "position",
        "strpos",
        "instr",
        "octet_length",
        "md5",
        "random",
    }
)


@dataclass
class Observation:
    """What one operation did on the scratch database."""

    statements: int = 0
    writes: list[str] = field(default_factory=list)
    """Statements that changed (or may have changed) something."""
    other_alias: bool = False
    """It executed a statement on another configured database."""

    @property
    def no_op(self) -> bool:
        return not self.writes and not self.other_alias


def classify(sql: str, rowcount: int | None) -> str | None:
    """None if the statement is harmless, else why it counts as a write."""
    text = _COMMENTS.sub(" ", sql).strip().rstrip(";").strip()
    if not text:
        return None
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if len(parts) > 1:
        for part in parts:
            reason = classify(part, None)
            if reason is not None:
                return reason
        return None
    upper = text.upper()
    keyword = upper.split(None, 1)[0]
    if keyword in _CONTROL:
        return None
    if keyword in ("SET",) and upper.startswith(("SET CONSTRAINTS", "SET LOCAL")):
        return None
    if keyword in ("UPDATE", "DELETE"):
        if rowcount == 0:
            return None
        return (
            f"{keyword} affected {rowcount if rowcount is not None else 'unknown'} rows"
        )
    if keyword in ("SELECT", "WITH", "VALUES"):
        if re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|INTO|NEXTVAL|SETVAL)\b", upper):
            return f"{keyword} statement that may write"
        for name in _CALL.findall(text):
            if name.lower().split(".")[-1] not in _PURE_FUNCTIONS:
                return f"calls {name}()"
        return None
    return f"{keyword} statement"


@contextmanager
def observe(alias: str, observation: Observation) -> Iterator[None]:
    """Record every statement executed on any configured connection."""

    def wrapper_for(name: str) -> Callable[..., Any]:
        def wrapper(
            execute: Callable[..., Any],
            sql: str,
            params: Any,
            many: bool,
            context: dict[str, Any],
        ) -> Any:
            result = execute(sql, params, many, context)
            observation.statements += 1
            if name != alias:
                observation.other_alias = True
            cursor = context.get("cursor")
            rowcount = getattr(cursor, "rowcount", None)
            reason = classify(str(sql), None if many else rowcount)
            if (
                many
                and reason is None
                and str(sql).strip().upper().startswith(("UPDATE", "DELETE"))
            ):
                reason = "executemany"
            if reason is not None:
                observation.writes.append(reason)
            return result

        return wrapper

    with ExitStack() as stack:
        for name in connections:
            stack.enter_context(connections[name].execute_wrapper(wrapper_for(name)))
        yield
