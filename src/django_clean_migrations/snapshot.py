"""
Snapshots of a database after migrating: schema (much more detailed than
Django's project state) and data, for comparing two runs of the history.

Incidental ordering is normalised away; names never are. Values of date and
time columns are masked, since they differ between runs.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db.backends.base.base import BaseDatabaseWrapper

_MASKED_FIELD_TYPES = frozenset(
    {"DateTimeField", "DateField", "TimeField", "DurationField"}
)
_PG_SYSTEM = "('pg_catalog', 'information_schema', 'pg_toast')"
_MASK = "<masked>"


@dataclass(frozen=True)
class Difference:
    table: str | None
    """The table concerned, or None for schema objects not tied to one."""
    kind: str
    """"schema" or "data"."""
    detail: str

    def __str__(self) -> str:
        where = f"table {self.table}" if self.table else "the schema"
        return f"{self.kind} of {where} differs ({self.detail})"


@dataclass
class Snapshot:
    vendor: str
    schema: dict[tuple[str, str | None, str], Any] = field(default_factory=dict)
    """(object kind, table or None, name) -> normalised definition."""
    data: dict[str, Counter[tuple[str, ...]]] = field(default_factory=dict)
    """table -> multiset of rows (values as repr strings)."""
    exact: bool = True
    """False when only Django's generic introspection was available."""

    def differences(
        self, other: Snapshot, ignore_data: Iterable[str] = ()
    ) -> list[Difference]:
        ignored = set(ignore_data)
        found: list[Difference] = []
        for key in sorted(self.schema.keys() | other.schema.keys(), key=repr):
            mine, theirs = self.schema.get(key), other.schema.get(key)
            if mine != theirs:
                what = (
                    "missing"
                    if theirs is None
                    else ("unexpected" if mine is None else "changed")
                )
                found.append(Difference(key[1], "schema", f"{key[0]} {key[2]} {what}"))
        for table in sorted(self.data.keys() | other.data.keys()):
            if table in ignored:
                continue
            mine_rows = self.data.get(table, Counter())
            theirs_rows = other.data.get(table, Counter())
            if mine_rows != theirs_rows:
                missing = sum((mine_rows - theirs_rows).values())
                extra = sum((theirs_rows - mine_rows).values())
                found.append(
                    Difference(
                        table, "data", f"{missing} row(s) missing, {extra} unexpected"
                    )
                )
        return found


def take_snapshot(connection: BaseDatabaseWrapper) -> Snapshot:
    snapshot = Snapshot(connection.vendor)
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            _sqlite_schema(connection, cursor, snapshot.schema)
        elif connection.vendor == "postgresql":
            _postgresql_schema(cursor, snapshot.schema)
        elif connection.vendor == "mysql":
            _mysql_schema(cursor, snapshot.schema)
        else:
            snapshot.exact = False
            _generic_schema(connection, cursor, snapshot.schema)
        _data(connection, cursor, snapshot.data)
    return snapshot


def _fetch(cursor: Any, sql: str, params: Iterable[Any] = ()) -> list[tuple[Any, ...]]:
    cursor.execute(sql, list(params))
    return [tuple(row) for row in cursor.fetchall()]


def _norm_sql(sql: Any) -> Any:
    return " ".join(sql.split()) if isinstance(sql, str) else sql


def _sqlite_schema(
    connection: Any, cursor: Any, schema: dict[tuple[str, str | None, str], Any]
) -> None:
    # Table SQL text depends on how the table was built (ALTER TABLE ADD
    # COLUMN vs a rebuild), so tables are compared through PRAGMAs instead.
    rows = _fetch(
        cursor,
        "SELECT type, name, tbl_name, sql FROM sqlite_master"
        " WHERE name NOT LIKE 'sqlite_%%'",
    )
    for kind, name, table, sql in rows:
        if kind == "table":
            q = connection.ops.quote_name(name)
            # By name: Django rebuilds SQLite tables in the project state's
            # field order, which depends on the history (RenameField and some
            # AlterFields move a field to the end), so the physical order of
            # columns is incidental there.
            schema[("columns", name, name)] = sorted(
                r[1:] for r in _fetch(cursor, f"PRAGMA table_xinfo({q})")
            )
            schema[("foreign keys", name, name)] = sorted(
                r[2:] for r in _fetch(cursor, f"PRAGMA foreign_key_list({q})")
            )
            for _, index, unique, origin, partial in _fetch(
                cursor, f"PRAGMA index_list({q})"
            ):
                columns = [
                    r[2]
                    for r in _fetch(
                        cursor,
                        f"PRAGMA index_xinfo({connection.ops.quote_name(index)})",
                    )
                ]
                key = (
                    index
                    if origin == "c"
                    else f"{origin}:{','.join(map(str, columns))}"
                )
                schema[("index", name, key)] = (unique, columns, partial)
            constraints = connection.introspection.get_constraints(cursor, name)
            for cname, info in constraints.items():
                if info["check"]:
                    label = "" if cname.startswith("__unnamed") else cname
                    schema[("check", name, f"{label}:{info['columns']}")] = True
        elif kind == "index" and sql:
            schema[("index sql", table, name)] = _norm_sql(sql)
        elif kind in ("trigger", "view"):
            schema[(kind, table, name)] = _norm_sql(sql)


def _postgresql_schema(
    cursor: Any, schema: dict[tuple[str, str | None, str], Any]
) -> None:
    columns = _fetch(
        cursor,
        "SELECT table_schema || '.' || table_name, column_name, ordinal_position,"
        " data_type, udt_name, character_maximum_length, numeric_precision,"
        " numeric_scale, datetime_precision, is_nullable, column_default,"
        " is_identity, identity_generation, collation_name, is_generated,"
        " generation_expression FROM information_schema.columns"
        f" WHERE table_schema NOT IN {_PG_SYSTEM}",
    )
    by_table: dict[str, list[tuple[Any, ...]]] = {}
    for row in columns:
        by_table.setdefault(row[0], []).append(row)
    for table, cols in by_table.items():
        # Dropped columns leave gaps in ordinal_position: compare the order.
        cols.sort(key=lambda r: r[2])
        schema[("columns", _table(table), table)] = [(r[1], *r[3:]) for r in cols]
    for table, name, contype, definition in _fetch(
        cursor,
        "SELECT n.nspname || '.' || cl.relname, c.conname, c.contype,"
        " pg_get_constraintdef(c.oid) FROM pg_constraint c"
        " JOIN pg_class cl ON cl.oid = c.conrelid"
        " JOIN pg_namespace n ON n.oid = cl.relnamespace"
        f" WHERE n.nspname NOT IN {_PG_SYSTEM}",
    ):
        schema[("constraint", _table(table), name)] = (contype, definition)
    for nsp, table, name, definition in _fetch(
        cursor,
        "SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes"
        f" WHERE schemaname NOT IN {_PG_SYSTEM}",
    ):
        schema[("index", table, f"{nsp}.{name}")] = definition
    for row in _fetch(
        cursor,
        "SELECT schemaname || '.' || sequencename, data_type, start_value,"
        " min_value, max_value, increment_by, cycle FROM pg_sequences"
        f" WHERE schemaname NOT IN {_PG_SYSTEM}",
    ):
        schema[("sequence", None, row[0])] = row[1:]
    for seq, table, column in _fetch(
        cursor,
        "SELECT s.relname, t.relname, a.attname FROM pg_class s"
        " JOIN pg_depend d ON d.objid = s.oid AND d.deptype IN ('a', 'i')"
        " JOIN pg_class t ON t.oid = d.refobjid"
        " JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid"
        " WHERE s.relkind = 'S'",
    ):
        schema[("sequence owner", table, seq)] = column
    for name, definition in _fetch(
        cursor,
        "SELECT schemaname || '.' || viewname, definition FROM pg_views"
        f" WHERE schemaname NOT IN {_PG_SYSTEM}",
    ):
        schema[("view", None, name)] = definition
    for name, definition in _fetch(
        cursor,
        "SELECT schemaname || '.' || matviewname, definition FROM pg_matviews",
    ):
        schema[("materialized view", None, name)] = definition
    for name, args, definition in _fetch(
        cursor,
        "SELECT n.nspname || '.' || p.proname,"
        " pg_get_function_identity_arguments(p.oid), pg_get_functiondef(p.oid)"
        " FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace"
        f" WHERE n.nspname NOT IN {_PG_SYSTEM} AND p.prokind IN ('f', 'p')",
    ):
        schema[("function", None, f"{name}({args})")] = definition
    for table, name, definition in _fetch(
        cursor,
        "SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid)"
        " FROM pg_trigger WHERE NOT tgisinternal",
    ):
        schema[("trigger", _table(table), name)] = definition
    for name, labels in _fetch(
        cursor,
        "SELECT n.nspname || '.' || t.typname,"
        " array_agg(e.enumlabel ORDER BY e.enumsortorder) FROM pg_type t"
        " JOIN pg_enum e ON e.enumtypid = t.oid"
        " JOIN pg_namespace n ON n.oid = t.typnamespace GROUP BY 1",
    ):
        schema[("enum", None, name)] = list(labels)
    for name, ext_version in _fetch(
        cursor, "SELECT extname, extversion FROM pg_extension"
    ):
        schema[("extension", None, name)] = ext_version
    for table, subid, description in _fetch(
        cursor,
        "SELECT n.nspname || '.' || c.relname, d.objsubid, d.description"
        " FROM pg_description d JOIN pg_class c ON c.oid = d.objoid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        f" WHERE n.nspname NOT IN {_PG_SYSTEM}",
    ):
        schema[("comment", _table(table), f"{table}:{subid}")] = description


def _table(qualified: str) -> str:
    return qualified.rsplit(".", 1)[-1].strip('"')


def _mysql_schema(cursor: Any, schema: dict[tuple[str, str | None, str], Any]) -> None:
    # Best effort, untested: information_schema of the current database.
    where = "WHERE table_schema = DATABASE()"
    cols: dict[str, list[tuple[Any, ...]]] = {}
    for row in _fetch(
        cursor,
        "SELECT table_name, column_name, ordinal_position, column_type,"
        " is_nullable, column_default, extra, collation_name, generation_expression"
        f" FROM information_schema.columns {where}",
    ):
        cols.setdefault(row[0], []).append(row)
    for table, rows in cols.items():
        rows.sort(key=lambda r: r[2])
        schema[("columns", table, table)] = [(r[1], *r[3:]) for r in rows]
    stats: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for table, name, non_unique, seq, column, sub_part, index_type in _fetch(
        cursor,
        "SELECT table_name, index_name, non_unique, seq_in_index, column_name,"
        f" sub_part, index_type FROM information_schema.statistics {where}",
    ):
        stats.setdefault((table, name), []).append(
            (seq, column, sub_part, non_unique, index_type)
        )
    for (table, name), parts in stats.items():
        schema[("index", table, name)] = sorted(parts)
    for table, name, ctype in _fetch(
        cursor,
        "SELECT table_name, constraint_name, constraint_type FROM"
        f" information_schema.table_constraints {where}",
    ):
        schema[("constraint", table, name)] = ctype
    for row in _fetch(
        cursor,
        "SELECT table_name, constraint_name, column_name, ordinal_position,"
        " referenced_table_name, referenced_column_name FROM"
        f" information_schema.key_column_usage {where}",
    ):
        schema[("key column", row[0], f"{row[1]}:{row[3]}")] = row[2:]
    for name, check in _fetch(
        cursor,
        "SELECT constraint_name, check_clause FROM information_schema.check_constraints"
        " WHERE constraint_schema = DATABASE()",
    ):
        schema[("check", None, name)] = check
    for table, name, timing, event, action in _fetch(
        cursor,
        "SELECT event_object_table, trigger_name, action_timing,"
        " event_manipulation, action_statement FROM information_schema.triggers"
        " WHERE trigger_schema = DATABASE()",
    ):
        schema[("trigger", table, name)] = (timing, event, action)
    for name, definition in _fetch(
        cursor,
        f"SELECT table_name, view_definition FROM information_schema.views {where}",
    ):
        schema[("view", None, name)] = definition
    for name, rtype, definition in _fetch(
        cursor,
        "SELECT routine_name, routine_type, routine_definition FROM"
        " information_schema.routines WHERE routine_schema = DATABASE()",
    ):
        schema[("routine", None, name)] = (rtype, definition)


def _generic_schema(
    connection: Any, cursor: Any, schema: dict[tuple[str, str | None, str], Any]
) -> None:
    introspection = connection.introspection
    for info in introspection.get_table_list(cursor):
        name = info.name
        schema[("table", name, name)] = info.type
        if info.type != "t":
            continue
        schema[("columns", name, name)] = [
            tuple(repr(v) for v in col)
            for col in introspection.get_table_description(cursor, name)
        ]
        for cname, cinfo in introspection.get_constraints(cursor, name).items():
            schema[("constraint", name, cname)] = repr(sorted(cinfo.items()))


def _masked_columns(connection: Any, cursor: Any, table: str) -> set[int]:
    masked: set[int] = set()
    try:
        description = connection.introspection.get_table_description(cursor, table)
    except Exception:
        return masked
    for i, col in enumerate(description):
        try:
            field_type = connection.introspection.get_field_type(col.type_code, col)
        except Exception:
            continue
        if isinstance(field_type, str) and field_type in _MASKED_FIELD_TYPES:
            masked.add(i)
    return masked


def _value(value: Any) -> str:
    if isinstance(value, memoryview):
        value = value.tobytes()
    return repr(value)


def _data(
    connection: Any, cursor: Any, data: dict[str, Counter[tuple[str, ...]]]
) -> None:
    quote = connection.ops.quote_name
    for table in connection.introspection.table_names(cursor):
        if table == "django_migrations":
            cursor.execute(f"SELECT app, name FROM {quote(table)}")
            data[table] = Counter(tuple(map(_value, r)) for r in cursor.fetchall())
            continue
        masked = _masked_columns(connection, cursor, table)
        cursor.execute(f"SELECT * FROM {quote(table)}")
        data[table] = Counter(
            tuple(
                _MASK if i in masked and v is not None else _value(v)
                for i, v in enumerate(row)
            )
            for row in cursor.fetchall()
        )
