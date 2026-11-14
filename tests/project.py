"""
Temporary Django projects with inline migration sources, plus the end-to-end
equivalence check: migrate a fresh SQLite database with the original and the
cleaned migrations and compare the introspected schemas.
"""

from __future__ import annotations

import importlib
import itertools
import sys
import textwrap
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from io import StringIO
from pathlib import Path
from typing import Any

from django.core.management import call_command
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from django_clean_migrations.core import Result, plan

IMPORTS = """\
import django.db.models.deletion
from django.db import migrations, models
"""

_counter = itertools.count()


def migration_source(
    operations: str, dependencies: list[tuple[str, str]], extra: str = ""
) -> str:
    """Build a migration file; ``operations`` is the list body, any indent."""
    ops = textwrap.indent(textwrap.dedent(operations).strip("\n"), " " * 8)
    deps = "".join(f"        {d!r},\n" for d in dependencies)
    parts = [IMPORTS]
    if extra:
        parts.append("\n" + textwrap.dedent(extra).strip("\n") + "\n")
    parts.append(
        "\n\nclass Migration(migrations.Migration):\n"
        f"    dependencies = [\n{deps}    ]\n\n"
        f"    operations = [\n{ops}\n    ]\n"
    )
    return "".join(parts)


class Project:
    def __init__(self, root: Path, db_blocker: Any) -> None:
        self.package = f"cleanmig_{next(_counter)}"
        self.root = root
        self.db_blocker = db_blocker
        (root / self.package).mkdir()
        (root / self.package / "__init__.py").write_text("")
        self.apps: list[str] = []
        self.last: dict[str, str] = {}
        self._stack: ExitStack | None = None
        sys.path.insert(0, str(root))

    # Building

    def path(self, app: str, name: str | None = None) -> Path:
        base = self.root / self.package / app / "migrations"
        return base if name is None else base / f"{name}.py"

    def _ensure_app(self, app: str) -> None:
        if app in self.apps:
            return
        app_dir = self.root / self.package / app
        (app_dir / "migrations").mkdir(parents=True)
        (app_dir / "__init__.py").write_text("")
        (app_dir / "migrations" / "__init__.py").write_text("")
        self.apps.append(app)
        if self._stack is not None:
            self.deactivate()

    def migration(
        self,
        app: str,
        name: str,
        operations: str = "",
        *,
        deps: list[tuple[str, str]] | None = None,
        extra_deps: list[tuple[str, str]] | None = None,
        extra: str = "",
        source: str | None = None,
    ) -> Path:
        """
        Add a migration. By default it depends on the previous migration of
        the same app; ``source`` gives the whole file verbatim.
        """
        self._ensure_app(app)
        if source is None:
            if deps is None:
                deps = [(app, self.last[app])] if app in self.last else []
            source = migration_source(operations, [*deps, *(extra_deps or [])], extra)
        else:
            source = textwrap.dedent(source).lstrip("\n")
        path = self.path(app, name)
        path.write_text(source)
        self.last[app] = name
        return path

    def read(self, app: str, name: str) -> str:
        return self.path(app, name).read_text()

    def files(self) -> dict[str, str]:
        return {
            f"{app}/{p.stem}": p.read_text()
            for app in self.apps
            for p in sorted(self.path(app).glob("0*.py"))
        }

    # Activation

    def purge(self) -> None:
        importlib.invalidate_caches()
        for name in list(sys.modules):
            if name == self.package or name.startswith(self.package + "."):
                del sys.modules[name]

    def activate(self) -> None:
        if self._stack is not None:
            return
        self.purge()
        stack = ExitStack()
        stack.enter_context(
            override_settings(
                INSTALLED_APPS=[
                    "django_clean_migrations",
                    *(f"{self.package}.{app}" for app in self.apps),
                ]
            )
        )
        self._stack = stack

    def deactivate(self) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None
        self.purge()

    def close(self) -> None:
        self.deactivate()
        try:
            sys.path.remove(str(self.root))
        except ValueError:
            pass

    # Running

    def clean(self, *targets: str, **options: Any) -> Result:
        self.activate()
        self.purge()
        return plan(targets, **options)

    def command(self, *args: str, **options: Any) -> tuple[str, str]:
        self.activate()
        self.purge()
        out, err = StringIO(), StringIO()
        call_command("clean_migrations", *args, stdout=out, stderr=err, **options)
        return out.getvalue(), err.getvalue()

    def migrate_schema(self) -> dict[str, Any]:
        """Migrate a fresh SQLite database from scratch and introspect it."""
        self.activate()
        self.purge()
        with self.db_blocker.unblock(), temp_database(self.root) as conn:
            executor = MigrationExecutor(conn)
            targets = executor.loader.graph.leaf_nodes()
            executor.migrate(targets)
            return introspect(conn)

    def check_equivalence(self, *targets: str, **options: Any) -> Result:
        """
        Clean and write, asserting the cleaned history migrates a fresh
        database to exactly the same schema as the original.
        """
        before = self.migrate_schema()
        result = self.clean(*targets, **options)
        result.write()
        after = self.migrate_schema()
        assert after == before
        return result


@contextmanager
def temp_database(root: Path) -> Iterator[Any]:
    alias = f"e2e_{next(_counter)}"
    path = root / f"{alias}.sqlite3"
    connections.settings[alias] = connections.configure_settings(
        {
            "default": {"ENGINE": "django.db.backends.sqlite3"},
            alias: {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)},
        }
    )[alias]
    try:
        yield connections[alias]
    finally:
        connections[alias].close()
        del connections[alias]
        del connections.settings[alias]


def _normalize_sql(sql: str | None) -> str | None:
    return " ".join(sql.split()) if sql else sql


def introspect(conn: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {}
    with conn.cursor() as c:
        c.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' AND name != 'django_migrations'"
        )
        tables = sorted(r[0] for r in c.fetchall())
        for table in tables:
            c.execute(f'PRAGMA table_info("{table}")')
            columns = [tuple(r[1:]) for r in c.fetchall()]
            c.execute(f'PRAGMA foreign_key_list("{table}")')
            fks = sorted(tuple(r[2:]) for r in c.fetchall())
            c.execute(f'PRAGMA index_list("{table}")')
            index_rows = c.fetchall()
            indexes = []
            for _, name, unique, origin, partial in index_rows:
                c.execute(f'PRAGMA index_info("{name}")')
                cols = tuple(r[2] for r in c.fetchall())
                c.execute("SELECT sql FROM sqlite_master WHERE name = %s", [name])
                row = c.fetchone()
                indexes.append(
                    (
                        name if origin == "c" else origin,
                        unique,
                        cols,
                        partial,
                        _normalize_sql(row[0] if row else None),
                    )
                )
            constraints = conn.introspection.get_constraints(c, table)
            checks = sorted(
                (
                    name if not name.startswith("__unnamed") else "",
                    tuple(info["columns"]),
                )
                for name, info in constraints.items()
                if info["check"]
            )
            schema[table] = {
                "columns": columns,
                "foreign_keys": fks,
                "indexes": sorted(indexes, key=repr),
                "checks": checks,
            }
    return schema
