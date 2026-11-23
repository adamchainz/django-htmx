"""
Snapshots of a database after migrating: schema (much more detailed than
Django's project state) and data, for comparing two runs of the history.

Ordering is normalised away, the order of a table's columns included; names
never are. The ids of content types and permissions are incidental too (they
follow which rows the history happened to create first, as after squashing),
so those rows, and foreign keys to them, are compared by natural key. The
next id a table gets (a sequence's value) isn't compared either. Values that differ
between two runs of the same history (times, random values) are found by
comparing two baseline runs (``Snapshot.nondeterminism``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db.backends.base.base import BaseDatabaseWrapper

CONTENT_TYPES = "django_content_type"
PERMISSIONS = "auth_permission"
_PG_SYSTEM = "('pg_catalog', 'information_schema', 'pg_toast')"


@dataclass(frozen=True)
class Difference:
    table: str | None
    """The table concerned, or None for schema objects not tied to one."""
    kind: str
    """"schema" or "data"."""
    detail: str
    columns: tuple[str, ...] = ()
    """For a table's columns: the names of those that differ."""

    def __str__(self) -> str:
        where = f"table {self.table}" if self.table else "the schema"
        return f"{self.kind} of {where} differs ({self.detail})"


@dataclass
class Mask:
    """What to leave out of comparisons: it differs between two runs of the
    same history (random or time-based values)."""

    tables: set[str] = field(default_factory=set)
    """Tables whose data isn't compared."""
    columns: dict[str, set[str]] = field(default_factory=dict)
    """table -> columns whose values aren't compared."""
    schema: set[tuple[str, str | None, str]] = field(default_factory=set)
    """Schema objects not compared."""

    def __bool__(self) -> bool:
        return bool(self.tables or self.columns or self.schema)

    def describe(self) -> str:
        parts = [f"data of {t}" for t in sorted(self.tables)]
        parts += [
            f"{t}.{c}" for t, cols in sorted(self.columns.items()) for c in sorted(cols)
        ]
        parts += [f"{k[0]} {k[2]}" for k in sorted(self.schema, key=repr)]
        return ", ".join(parts)


@dataclass
class Snapshot:
    vendor: str
    schema: dict[tuple[str, str | None, str], Any] = field(default_factory=dict)
    """(object kind, table or None, name) -> normalised definition."""
    data: dict[str, Counter[tuple[str, ...]]] = field(default_factory=dict)
    """table -> multiset of rows (values as repr strings, columns in the
    order of ``columns``)."""
    columns: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """table -> its column names, sorted."""

    def differences(
        self,
        other: Snapshot,
        mask: Mask | None = None,
    ) -> list[Difference]:
        mask = mask or Mask()
        ignored = mask.tables
        found: list[Difference] = []
        for key in sorted(self.schema.keys() | other.schema.keys(), key=repr):
            if key in mask.schema:
                continue
            mine, theirs = self.schema.get(key), other.schema.get(key)
            if mine != theirs:
                what = (
                    "missing"
                    if theirs is None
                    else ("unexpected" if mine is None else "changed")
                )
                columns = (
                    _column_changes(mine, theirs)
                    if key[0] == "columns" and mine and theirs
                    else ()
                )
                detail = f"{key[0]} {key[2]} {what}"
                if columns:
                    detail += f": {', '.join(columns)}"
                found.append(Difference(key[1], "schema", detail, columns))
        for table in sorted(self.data.keys() | other.data.keys()):
            if table in ignored:
                continue
            mine_rows = self._rows(table, mask.columns.get(table, set()))
            theirs_rows = other._rows(table, mask.columns.get(table, set()))
            if mine_rows != theirs_rows:
                missing = sum((mine_rows - theirs_rows).values())
                extra = sum((theirs_rows - mine_rows).values())
                found.append(
                    Difference(
                        table, "data", f"{missing} row(s) missing, {extra} unexpected"
                    )
                )
        return found

    def without_migrations(self, keys: Iterable[tuple[str, str]]) -> Snapshot:
        """A copy without the django_migrations rows of ``keys`` (pruned
        migrations, which a run of the pruned history doesn't record)."""
        rows = self.data.get("django_migrations")
        if rows is None:
            return self
        data = dict(self.data)
        data["django_migrations"] = rows - Counter(
            (_value(app), _value(name)) for app, name in keys
        )
        return Snapshot(self.vendor, self.schema, data, self.columns)

    def _rows(self, table: str, without: set[str]) -> Counter[tuple[str, ...]]:
        rows = self.data.get(table, Counter())
        if not without:
            return rows
        names = self.columns.get(table, ())
        keep = [i for i, name in enumerate(names) if name not in without]
        projected: Counter[tuple[str, ...]] = Counter()
        for row, n in rows.items():
            projected[tuple(row[i] for i in keep)] += n
        return projected

    def nondeterminism(self, again: Snapshot) -> Mask:
        """
        What differs between this snapshot and ``again``, of the same history:
        columns whose values differ (if leaving them out makes the rows
        match), else whole tables, and schema objects.
        """
        mask = Mask()
        for key in self.schema.keys() | again.schema.keys():
            if self.schema.get(key) != again.schema.get(key):
                mask.schema.add(key)
        for table in self.data.keys() | again.data.keys():
            mine, theirs = self.data.get(table), again.data.get(table)
            if mine == theirs:
                continue
            names = self.columns.get(table)
            if mine is None or theirs is None or names != again.columns.get(table):
                mask.tables.add(table)
                continue
            assert names is not None
            differing = {
                name
                for i, name in enumerate(names)
                if Counter(r[i] for r in mine.elements())
                != Counter(r[i] for r in theirs.elements())
            }
            if differing and self._rows(table, differing) == again._rows(
                table, differing
            ):
                mask.columns[table] = differing
            else:
                mask.tables.add(table)
        return mask


def _column_changes(mine: Any, theirs: Any) -> tuple[str, ...]:
    """Names of the columns that differ between two column lists (name
    first in each entry)."""
    a = {str(c[0]): c for c in mine}
    b = {str(c[0]): c for c in theirs}
    return tuple(sorted(n for n in a.keys() | b.keys() if a.get(n) != b.get(n)))


def take_snapshot(connection: BaseDatabaseWrapper) -> Snapshot:
    snapshot = Snapshot(connection.vendor)
    with connection.cursor() as cursor:
        if connection.vendor == "sqlite":
            _sqlite_schema(connection, cursor, snapshot.schema)
        elif connection.vendor == "postgresql":
            _postgresql_schema(cursor, snapshot.schema)
        else:
            # Scratch databases exist only for these three vendors.
            assert connection.vendor == "mysql"
            _mysql_schema(cursor, snapshot.schema)
        _data(connection, cursor, snapshot.data, snapshot.columns)
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
            xinfo = _fetch(cursor, f"PRAGMA table_xinfo({q})")
            schema[("columns", name, name)] = sorted(r[1:] for r in xinfo)
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
        elif kind == "index":  # sqlite_autoindex ones (no SQL) are left out above
            schema[("index sql", table, name)] = _norm_sql(sql)
        else:  # triggers and views
            schema[(kind, table, name)] = _norm_sql(sql)


def _postgresql_schema(
    cursor: Any, schema: dict[tuple[str, str | None, str], Any]
) -> None:
    columns = _fetch(
        cursor,
        "SELECT table_schema || '.' || table_name, column_name, data_type,"
        " udt_name, character_maximum_length, numeric_precision,"
        " numeric_scale, datetime_precision, is_nullable, column_default,"
        " is_identity, identity_generation, collation_name, is_generated,"
        " generation_expression FROM information_schema.columns"
        f" WHERE table_schema NOT IN {_PG_SYSTEM}",
    )
    by_table: dict[str, list[tuple[Any, ...]]] = {}
    for row in columns:
        by_table.setdefault(row[0], []).append(row)
    for table, cols in by_table.items():
        schema[("columns", _table(table), table)] = sorted(r[1:] for r in cols)
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
    # By column name: a column's number depends on those created before it.
    for table, column, description in _fetch(
        cursor,
        "SELECT n.nspname || '.' || c.relname, coalesce(a.attname, ''),"
        " d.description"
        " FROM pg_description d JOIN pg_class c ON c.oid = d.objoid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " LEFT JOIN pg_attribute a"
        " ON a.attrelid = d.objoid AND a.attnum = d.objsubid AND d.objsubid <> 0"
        f" WHERE n.nspname NOT IN {_PG_SYSTEM}",
    ):
        schema[("comment", _table(table), f"{table}:{column}")] = description


def _table(qualified: str) -> str:
    return qualified.rsplit(".", 1)[-1].strip('"')


def _rows(cursor: Any, sql: str) -> list[dict[str, Any]]:
    """Rows as dicts by lower-case column name: MySQL and MariaDB have
    different information_schema columns."""
    cursor.execute(sql)
    names = [str(d[0]).lower() for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def _mysql_schema(cursor: Any, schema: dict[tuple[str, str | None, str], Any]) -> None:
    """What mysqldump --no-data shows, from information_schema."""
    where = "WHERE table_schema = DATABASE()"
    for t in _rows(
        cursor,
        "SELECT table_name, engine, table_collation, table_comment, create_options"
        f" FROM information_schema.tables {where} AND table_type = 'BASE TABLE'",
    ):
        schema[("table", t["table_name"], t["table_name"])] = (
            t["engine"],
            t["table_collation"],
            t["table_comment"],
            t["create_options"],
        )
    cols: dict[str, list[dict[str, Any]]] = {}
    for c in _rows(cursor, f"SELECT * FROM information_schema.columns {where}"):
        cols.setdefault(c["table_name"], []).append(c)
    for table, rows in cols.items():
        rows.sort(key=lambda c: c["column_name"])
        schema[("columns", table, table)] = [
            (
                c["column_name"],
                c["column_type"],
                c["is_nullable"],
                c["column_default"],
                c["extra"],
                c["collation_name"],
                c["column_comment"],
                c.get("generation_expression"),
            )
            for c in rows
        ]
    stats: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for i in _rows(cursor, f"SELECT * FROM information_schema.statistics {where}"):
        stats.setdefault((i["table_name"], i["index_name"]), []).append(
            (
                i["seq_in_index"],
                i["column_name"],
                i.get("expression"),  # MySQL's functional indexes
                i["sub_part"],
                i["collation"],
                i["non_unique"],
                i["index_type"],
                i["index_comment"],
                i.get("is_visible", i.get("ignored")),
            )
        )
    for (table, name), parts in stats.items():
        schema[("index", table, name)] = sorted(parts, key=lambda p: p[0])
    check_tables: dict[str, str] = {}
    for c in _rows(
        cursor,
        "SELECT table_name, constraint_name, constraint_type FROM"
        f" information_schema.table_constraints {where}",
    ):
        schema[("constraint", c["table_name"], c["constraint_name"])] = c[
            "constraint_type"
        ]
        if c["constraint_type"] == "CHECK":
            check_tables[c["constraint_name"]] = c["table_name"]
    for k in _rows(
        cursor,
        "SELECT table_name, constraint_name, column_name, ordinal_position,"
        " referenced_table_name, referenced_column_name FROM"
        f" information_schema.key_column_usage {where}",
    ):
        schema[
            (
                "key column",
                k["table_name"],
                f"{k['constraint_name']}:{k['ordinal_position']}",
            )
        ] = (k["column_name"], k["referenced_table_name"], k["referenced_column_name"])
    for r in _rows(
        cursor,
        "SELECT table_name, constraint_name, update_rule, delete_rule,"
        " unique_constraint_name FROM information_schema.referential_constraints"
        " WHERE constraint_schema = DATABASE()",
    ):
        schema[("foreign key", r["table_name"], r["constraint_name"])] = (
            r["update_rule"],
            r["delete_rule"],
            r["unique_constraint_name"],
        )
    for c in _rows(
        cursor,
        "SELECT * FROM information_schema.check_constraints"
        " WHERE constraint_schema = DATABASE()",
    ):
        # MariaDB names a column's check after the column: its table tells
        # them apart (MySQL's names are unique in the database).
        owner = c.get("table_name") or check_tables.get(c["constraint_name"])
        schema[("check", owner, c["constraint_name"])] = c["check_clause"]
    for t in _rows(
        cursor,
        "SELECT event_object_table, trigger_name, action_timing,"
        " event_manipulation, action_statement FROM information_schema.triggers"
        " WHERE trigger_schema = DATABASE()",
    ):
        schema[("trigger", t["event_object_table"], t["trigger_name"])] = (
            t["action_timing"],
            t["event_manipulation"],
            t["action_statement"],
        )
    for v in _rows(
        cursor,
        f"SELECT table_name, view_definition FROM information_schema.views {where}",
    ):
        schema[("view", None, v["table_name"])] = v["view_definition"]
    for r in _rows(
        cursor,
        "SELECT routine_name, routine_type, routine_definition FROM"
        " information_schema.routines WHERE routine_schema = DATABASE()",
    ):
        schema[("routine", None, r["routine_name"])] = (
            r["routine_type"],
            r["routine_definition"],
        )


def _value(value: Any) -> str:
    if isinstance(value, memoryview):
        value = value.tobytes()
    return repr(value)


def _data(
    connection: Any,
    cursor: Any,
    data: dict[str, Counter[tuple[str, ...]]],
    columns: dict[str, tuple[str, ...]],
) -> None:
    quote = connection.ops.quote_name
    tables = connection.introspection.table_names(cursor)
    natural = _natural_keys(cursor, quote, tables)
    for table in tables:
        if table == "django_migrations":
            cursor.execute(f"SELECT app, name FROM {quote(table)}")
            data[table] = Counter(tuple(map(_value, r)) for r in cursor.fetchall())
            columns[table] = ("app", "name")
            continue
        cursor.execute(f"SELECT * FROM {quote(table)}")
        names = [str(d[0]) for d in cursor.description]
        order = sorted(range(len(names)), key=lambda i: names[i])
        columns[table] = tuple(names[i] for i in order)
        rows = cursor.fetchall()
        # column -> the natural keys of the ids it holds.
        keyed = {"id": natural[table]} if table in natural else {}
        if natural:
            relations = connection.introspection.get_relations(cursor, table)
            for column, relation in relations.items():
                if relation[0] == "id" and relation[1] in natural:
                    keyed[column] = natural[relation[1]]
        maps = [keyed.get(names[i]) for i in order]
        data[table] = Counter(
            tuple(
                _value(row[i] if keys is None else keys.get(row[i], row[i]))
                for i, keys in zip(order, maps, strict=True)
            )
            for row in rows
        )


def _natural_keys(
    cursor: Any, quote: Any, tables: Iterable[str]
) -> dict[str, dict[Any, str]]:
    """table -> id -> natural key, for content types and permissions."""
    natural: dict[str, dict[Any, str]] = {}
    if CONTENT_TYPES not in tables:
        return natural
    types = natural[CONTENT_TYPES] = {
        pk: f"<content type {app_label}.{model}>"
        for pk, app_label, model in _fetch(
            cursor, f"SELECT id, app_label, model FROM {quote(CONTENT_TYPES)}"
        )
    }
    if PERMISSIONS in tables:
        natural[PERMISSIONS] = {
            pk: f"<permission {types.get(type_id, type_id)} {codename}>"
            for pk, type_id, codename in _fetch(
                cursor,
                f"SELECT id, content_type_id, codename FROM {quote(PERMISSIONS)}",
            )
        }
    return natural
