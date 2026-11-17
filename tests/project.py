"""
Temporary Django projects with inline migration sources, plus the end-to-end
equivalence check: migrate a fresh SQLite database with the original and the
compacted migrations and compare the introspected schemas.
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

from django.conf import settings
from django.core.management import call_command
from django.db import connections
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from django_compact_migrations.core import Result, plan
from django_compact_migrations.trial import TrialFailure

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
        self.package = f"compactmig_{next(_counter)}"
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
                    "django_compact_migrations",
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

    def compact(self, *targets: str, **options: Any) -> Result:
        """plan(), static unless ``static=False`` (or ``noop_data``): most
        tests are about the analysis, which a trial would only slow down."""
        self.activate()
        self.purge()
        with self.db_blocker.unblock():
            options.setdefault("static", not options.get("noop_data"))
            return plan(targets, **options)

    def command(self, *args: str, **options: Any) -> tuple[str, str]:
        self.activate()
        self.purge()
        out, err = StringIO(), StringIO()
        with self.db_blocker.unblock():
            call_command("compact_migrations", *args, stdout=out, stderr=err, **options)
        return out.getvalue(), err.getvalue()

    def command_db(self, *args: str, **options: Any) -> tuple[str, str]:
        """Like command(), allowing database access (scratch databases)."""
        with self.db_blocker.unblock():
            return self.command(*args, **options)

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
        Compact and write, asserting the compacted history migrates a fresh
        database to exactly the same schema as the original.
        """
        before = self.migrate_schema()
        result = self.compact(*targets, **options)
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


ITEM = """
    migrations.CreateModel(
        name="Item",
        fields=[
            ("id", models.AutoField(primary_key=True)),
            ("name", models.CharField(max_length=10)),
        ],
    ),
"""

# Folding is on by default. Tests about what is removed turn it off with
# this, since folds also rewrite what is kept.
NO_FOLD: dict[str, Any] = {"skip_kinds": ["fold"]}


def kinds(result: Result) -> list[tuple[str, str]]:
    return sorted((r.kind, r.label) for r in result.removals)


def blocked(result: Result) -> dict[str, tuple[str, ...]]:
    return {b.label: b.reasons for b in result.blocked}


def folds(result: Result) -> list[tuple[str, str]]:
    return sorted((r.label, r.description) for r in result.removals if r.kind == "fold")


def with_data_migration(project: Project, code: str, extra_ops: str = "") -> None:
    """shop.Item.legacy, added then removed, with a RunPython running
    ``code`` (``Item`` and ``alias`` defined) in between."""
    project.migration("shop", "0001_initial", ITEM)
    project.migration(
        "shop",
        "0002_add",
        'migrations.AddField("item", "legacy", models.IntegerField(default=0)),\n'
        + extra_ops
        + "\nmigrations.RunPython(forwards),",
        extra="def forwards(apps, schema_editor):\n"
        "    Item = apps.get_model('shop', 'Item')\n"
        "    alias = schema_editor.connection.alias\n"
        + "\n".join(f"    {line}" for line in code.strip().splitlines()),
    )
    project.migration(
        "shop", "0003_remove", 'migrations.RemoveField("item", "legacy"),'
    )


# Code for with_data_migration() that mentions legacy but doesn't need it.
MENTIONED = """
for item in Item.objects.using(alias).all():
    item.legacy = 1
"""

# A failure a scripted trial run reports.
FAILURE = TrialFailure(("shop", "0002_add"), "RunPython", "boom", "", True)


# The database servers of compose.yaml.

SERVERS: dict[str, dict[str, Any]] = {
    "pg": {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": "localhost",
        "PORT": "55432",
        "USER": "postgres",
        "PASSWORD": "postgres",
    },
    "mysql": {
        "ENGINE": "django.db.backends.mysql",
        "HOST": "127.0.0.1",
        "PORT": "53306",
        "USER": "root",
        "PASSWORD": "mysql",
        "OPTIONS": {"charset": "utf8mb4"},
    },
    "mariadb": {
        "ENGINE": "django.db.backends.mysql",
        "HOST": "127.0.0.1",
        "PORT": "53307",
        "USER": "root",
        "PASSWORD": "mariadb",
        "OPTIONS": {"charset": "utf8mb4"},
    },
}


@contextmanager
def server_alias(alias: str, db_blocker: Any) -> Iterator[str]:
    """A database alias for a server of compose.yaml (its own database is
    only used for scratch databases' names)."""
    config = {**SERVERS[alias], "NAME": "compactdev_tests"}
    if config["ENGINE"].endswith("mysql"):
        config["NAME"] = "mysql"  # exists on every MySQL-family server
    connections.settings[alias] = connections.configure_settings(
        {"default": {"ENGINE": "django.db.backends.sqlite3"}, alias: config}
    )[alias]
    settings.DATABASES[alias] = connections.settings[alias]
    try:
        with db_blocker.unblock():
            yield alias
    finally:
        connections[alias].close()
        del connections[alias]
        connections.settings.pop(alias, None)
        settings.DATABASES.pop(alias, None)
