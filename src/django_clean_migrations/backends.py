"""
Backends whose DDL depends on the history, not only on the final state.

The static analysis reasons about the project state, which is the same for
every backend. On MySQL and MariaDB, two things in the database also depend
on how it got there, so removals and folds that the state says are harmless
can change the schema:

- **The index backing a foreign key.** InnoDB needs an index whose first
  column is a foreign key's column. Creating the foreign key creates one
  implicitly, named like the constraint (``..._fk_<table>_<column>``),
  unless an index starting with that column already exists; MySQL drops
  that implicit index when another index starting with the column is
  created (a ``unique_together``, an ``Index``, a ``UniqueConstraint``, a
  unique or indexed field); and Django, before dropping such an index,
  creates its own one on the column (``_create_missing_fk_index()``), named
  ``<table>_<column>_<hash>``. So which index backs the foreign key, and its
  name, depends on that history.
- **Defaults of TEXT, BLOB and JSON columns, on MySQL.** ``AddField`` of such
  a field with a default adds the column with ``DEFAULT (<expression>)`` and
  never drops it (MySQL can't ``ALTER COLUMN ... DROP DEFAULT`` there, so
  Django skips it), while ``CreateModel`` creates the column without a
  default, and ``AlterField`` changes it only when it redefines the column.
  MariaDB drops the default as on other backends.

The plan is made without knowing which database production runs, so these
rules apply when any configured database is MySQL or MariaDB (see
configured()); plans for PostgreSQL and SQLite projects are unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from django.db import connections, models
from django.db.models.expressions import F

MYSQL = "mysql"
MARIADB = "mariadb"

# Internal types Django stores as TEXT, BLOB or JSON on MySQL
# (DatabaseWrapper._limited_data_types).
_LIMITED_TYPES = frozenset({"TextField", "BinaryField", "JSONField"})


def configured() -> frozenset[str]:
    """
    The MySQL-family servers among the configured databases: "mysql" and/or
    "mariadb". Telling them apart needs the server's version: a server that
    can't be reached counts as MySQL, whose rules are a superset.
    """
    found: set[str] = set()
    for alias in connections:
        connection = connections[alias]
        if connection.vendor != "mysql":
            continue
        try:
            mariadb = connection.mysql_is_mariadb  # type: ignore[attr-defined]
            found.add(MARIADB if mariadb else MYSQL)
        except Exception:
            found.add(MYSQL)
    return frozenset(found)


def describe(backends: Iterable[str]) -> str:
    names = {MYSQL: "MySQL", MARIADB: "MariaDB"}
    return " and ".join(names[b] for b in sorted(backends))


def is_foreign_key(field: Any) -> bool:
    return isinstance(field, models.ForeignKey)


def index_shape(field: Any) -> tuple[bool, bool, bool, bool]:
    """What decides which index backs a column on MySQL."""
    return (
        is_foreign_key(field),
        bool(getattr(field, "db_constraint", False)),
        bool(getattr(field, "unique", False)),
        bool(getattr(field, "db_index", False)),
    )


def leaves_default(field: Any) -> bool:
    """Whether MySQL's AddField leaves a DEFAULT on this field's column."""
    if field.get_internal_type() not in _LIMITED_TYPES or not field.has_default():
        return False
    # An empty default isn't set on the column (skip_default()).
    return field.get_default() not in ("", b"")


def first_field_name(obj: Any) -> str | None:
    """The first column of an index or unique constraint, by field name."""
    fields = getattr(obj, "fields", None)
    if fields:
        return str(fields[0]).lstrip("-")
    expressions = getattr(obj, "expressions", None)
    if expressions:
        first = getattr(expressions[0], "expression", expressions[0])
        if isinstance(first, F):
            return str(first.name)  # type: ignore[attr-defined]
    return None
