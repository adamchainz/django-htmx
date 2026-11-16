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

import ast
import inspect
import re
import textwrap
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
# Functions Django's ORM puts in SELECT lists that can't change anything.
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
    tables: set[str] = field(default_factory=set)
    """Tables its writes name (lower case); "*" if one names none."""

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
            # executemany(): the row count isn't per statement, so unknown.
            reason = classify(str(sql), None if many else rowcount)
            if reason is not None:
                observation.writes.append(reason)
                observation.tables |= written_tables(str(sql))
            return result

        return wrapper

    with ExitStack() as stack:
        for name in connections:
            stack.enter_context(connections[name].execute_wrapper(wrapper_for(name)))
        yield


# What a RunPython function may do that no query shows: register signal
# handlers, branch on the backend (and so do something else elsewhere),
# reset sequences, touch files, the network, caches or other processes.
_SUSPECT_NAMES = frozenset(
    {
        "post_migrate",
        "pre_migrate",
        "receiver",
        "call_command",
        "open",
        "subprocess",
        "os",
        "shutil",
        "Path",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "cache",
        "caches",
        "send_mail",
        "send_mass_mail",
        "management",
        "signals",
    }
)
_SUSPECT_ATTRIBUTES = frozenset(
    {
        "connect",
        "vendor",
        "ops",
        "features",
        "sequence_reset_sql",
        "sequence_reset_by_name_sql",
        "call_command",
        "send",
        "send_robust",
    }
)
_SUSPECT_MODULES = frozenset(
    {
        "os",
        "subprocess",
        "shutil",
        "socket",
        "requests",
        "httpx",
        "urllib",
        "pathlib",
        "django.core.management",
        "django.core.cache",
        "django.core.mail",
        "django.db.models.signals",
        "django.dispatch",
    }
)
# Calls a function that ran no query at all may make: anything else did
# something that isn't in the database, or nothing observable.
_QUIET_CALLS = frozenset(
    {
        "get_model",
        "get_models",
        "get_app_config",
        "len",
        "print",
        "range",
        "list",
        "dict",
        "set",
        "tuple",
        "str",
        "int",
        "bool",
        "isinstance",
        "getattr",
        "hasattr",
        "enumerate",
        "zip",
        "sorted",
        "format",
        "join",
        "using",
        "info",
        "debug",
        "warning",
        "getLogger",
    }
)


def _functions(func: Any) -> list[ast.AST] | None:
    """The AST of ``func`` and of the module-level functions it calls by
    name (one level), or None if its source can't be read."""
    func = inspect.unwrap(func)
    try:
        source = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError, IndentationError):
        return None
    trees: list[ast.AST] = [tree]
    module_globals = getattr(func, "__globals__", {})
    module = module_globals.get("__name__")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            helper = module_globals.get(node.func.id)
            if (
                inspect.isfunction(helper)
                and helper is not func
                and helper.__module__ == module
            ):
                try:
                    trees.append(ast.parse(textwrap.dedent(inspect.getsource(helper))))
                except (OSError, TypeError, SyntaxError, IndentationError):
                    return None
    return trees


def _call_name(node: ast.Call) -> str:
    target = node.func
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def source_concern(func: Any, statements: int) -> str | None:
    """
    Why a RunPython function that wrote nothing on the trial database may
    still matter, from its source; None if nothing is suspect.
    """
    trees = _functions(func)
    if trees is None:
        return "its source can't be read"
    for tree in trees:
        local = _bound_names(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Name)
                and node.id in _SUSPECT_NAMES
                and node.id not in local
            ):
                return f"it uses {node.id}"
            if isinstance(node, ast.Attribute) and node.attr in _SUSPECT_ATTRIBUTES:
                return f"it uses .{node.attr}"
            if isinstance(node, ast.Import | ast.ImportFrom):
                modules = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [a.name for a in node.names]
                )
                for module in modules:
                    if module.split(".")[0] in _SUSPECT_MODULES or module in (
                        _SUSPECT_MODULES
                    ):
                        return f"it imports {module}"
    if statements == 0:
        for tree in trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = _call_name(node)
                    if name in _QUIET_CALLS or name in _local_functions(trees):
                        continue
                    return f"it ran no query, yet calls {name or 'something'}()"
    return None


def _local_functions(trees: list[ast.AST]) -> set[str]:
    return {
        node.name
        for tree in trees
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


def _bound_names(tree: ast.AST) -> set[str]:
    """Names the function binds itself (a local ``cache = {}`` is fine)."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return bound


_WRITTEN = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|COPY|INTO|TRUNCATE(?:\s+TABLE)?)"
    r"\s+(?:ONLY\s+)?([`\"\[]?[\w.]+[`\"\]]?)",
    re.I,
)


def written_tables(sql: str) -> set[str]:
    """The tables a write statement names; {"*"} if it names none."""
    found = {
        m.group(1).strip('`"[]').lower().rsplit(".", 1)[-1]
        for m in _WRITTEN.finditer(_COMMENTS.sub(" ", sql))
    }
    return found or {"*"}
