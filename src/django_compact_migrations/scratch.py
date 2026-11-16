"""
Throwaway databases for running the migration history for real.

While one exists, *every* configured database alias is pointed at a scratch
database, as Django's test runner does, so project code run by migrations
(data migrations, signal handlers) can't reach a real database through any
alias: the chosen alias, the aliases sharing its database (same engine,
host, port and name) and its test mirrors share one scratch database; every
other database gets its own empty one. Only the chosen alias is migrated.
"""

from __future__ import annotations

import secrets
import shutil
import signal
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import connections
from django.db.backends.base.base import BaseDatabaseWrapper

PREFIX = "compactmig_"
_CREATE_DATABASE_VENDORS = frozenset({"postgresql", "mysql"})
_DUMMY_ENGINE = "django.db.backends.dummy"


class ScratchDatabaseError(Exception):
    pass


def _scratch_name(real_name: str, max_length: int) -> str:
    # A distinct prefix (not Django's "test_") so a test database kept with
    # --keepdb is never clobbered; a random part so runs never collide.
    name = f"{PREFIX}{secrets.token_hex(4)}_{real_name}"
    return name[:max_length]


@dataclass
class _Group:
    """Aliases that reach the same real database."""

    aliases: list[str] = field(default_factory=list)
    scratch: str = ""
    temp_dir: str | None = None
    created: bool = False


def _signature(settings_dict: dict[str, Any]) -> tuple[Any, ...]:
    return (
        settings_dict.get("ENGINE"),
        settings_dict.get("HOST") or "",
        str(settings_dict.get("PORT") or ""),
        settings_dict.get("NAME"),
    )


def _groups(alias: str) -> list[_Group]:
    """
    Group every configured alias by the database it reaches, like
    django.test.utils.get_unique_databases_and_mirrors(): aliases with the
    same engine, host, port and name, and test mirrors (TEST["MIRROR"]),
    share one group. The chosen alias's group comes first.
    """
    by_signature: dict[tuple[Any, ...], _Group] = {}
    mirrors: dict[str, str] = {}
    for name in connections:
        settings_dict = connections[name].settings_dict
        mirror = (settings_dict.get("TEST") or {}).get("MIRROR")
        if mirror:
            if mirror not in connections:
                raise ScratchDatabaseError(
                    f"Database alias {name!r} mirrors {mirror!r}, which isn't"
                    " configured."
                )
            mirrors[name] = mirror
            continue
        by_signature.setdefault(_signature(settings_dict), _Group()).aliases.append(
            name
        )
    groups = list(by_signature.values())
    for name, target in mirrors.items():
        while target in mirrors:
            target = mirrors[target]
        next(g for g in groups if target in g.aliases).aliases.append(name)
    groups.sort(key=lambda g: alias not in g.aliases)
    return groups


@contextmanager
def scratch_database(alias: str = "default") -> Iterator[BaseDatabaseWrapper]:
    """
    Create empty scratch databases, point every alias at one (see the module
    docstring), yield the connection of ``alias``, and always close every
    connection, restore the settings and destroy the databases afterwards,
    even on KeyboardInterrupt or SIGTERM.
    """
    if alias not in connections:
        raise ScratchDatabaseError(f"There is no database alias {alias!r}.")
    groups = _groups(alias)
    for group in groups:
        first = connections[group.aliases[0]]
        if first.settings_dict.get("ENGINE") == _DUMMY_ENGINE:
            if alias in group.aliases:
                raise ScratchDatabaseError(
                    f"Database alias {alias!r} uses the dummy backend."
                )
            continue  # Can't reach any database: harmless.
        if first.vendor not in _CREATE_DATABASE_VENDORS | {"sqlite"}:
            names = ", ".join(repr(a) for a in group.aliases)
            raise ScratchDatabaseError(
                f"Scratch databases aren't supported for the {first.vendor!r}"
                f" backend (database alias {names}). Every configured alias is"
                " pointed at a scratch database while migrations run, so that"
                " project code can't reach a real database; remove that alias"
                " from DATABASES for this command."
            )

    saved: dict[str, tuple[Any, Any]] = {}
    kept_open: dict[str, Any] = {}
    restore_signals = _exit_on_termination()
    try:
        close_everything()
        for name in connections:
            # close() leaves in-memory SQLite databases open (closing would
            # destroy them): set them aside so the alias reconnects.
            if connections[name].connection is not None:
                kept_open[name] = connections[name].connection
                connections[name].connection = None
        for group in groups:
            first = connections[group.aliases[0]]
            if first.settings_dict.get("ENGINE") == _DUMMY_ENGINE:
                continue
            real_name = first.settings_dict["NAME"]
            if first.vendor == "sqlite":
                group.temp_dir = tempfile.mkdtemp(prefix=PREFIX)
                group.scratch = str(Path(group.temp_dir) / "scratch.sqlite3")
            else:
                max_length = getattr(first.ops, "max_name_length", lambda: 63)() or 63
                group.scratch = _scratch_name(
                    str(real_name or ""), min(int(max_length), 63)
                )
                if group.scratch == real_name:
                    raise ScratchDatabaseError(
                        f"Refusing to use {group.scratch!r}: it is a real"
                        " database's name."
                    )
                _create(first, group.scratch)
                group.created = True
            for name in group.aliases:
                settings_dict = connections[name].settings_dict
                configured = settings.DATABASES.get(name)
                saved[name] = (
                    settings_dict["NAME"],
                    configured.get("NAME") if configured is not None else None,
                )
                settings_dict["NAME"] = group.scratch
                if configured is not None:
                    configured["NAME"] = group.scratch
        yield connections[alias]
    finally:
        try:
            close_everything()
        finally:
            for name, kept in kept_open.items():
                connections[name].connection = kept
            for name, (real, configured_name) in saved.items():
                connections[name].settings_dict["NAME"] = real
                configured = settings.DATABASES.get(name)
                if configured is not None:
                    configured["NAME"] = configured_name
            errors: list[str] = []
            for group in groups:
                if group.created:
                    try:
                        _drop(connections[group.aliases[0]], group.scratch)
                    except ScratchDatabaseError as exc:
                        errors.append(str(exc))
                if group.temp_dir is not None:
                    shutil.rmtree(group.temp_dir, ignore_errors=True)
            restore_signals()
            if errors:
                raise ScratchDatabaseError(" ".join(errors))


def close_everything() -> None:
    """Close every connection (and connection pool) of every alias."""
    for name in connections:
        connection = connections[name]
        connection.close()
        close_pool = getattr(connection, "close_pool", None)
        if close_pool is not None:
            close_pool()


def _create(connection: Any, name: str) -> None:
    creation = connection.creation
    params = {
        "dbname": connection.ops.quote_name(name),
        "suffix": creation.sql_table_creation_suffix(),
    }
    try:
        with creation._nodb_cursor() as cursor:
            cursor.execute("CREATE DATABASE {dbname} {suffix}".format(**params))
    except Exception as exc:
        raise ScratchDatabaseError(
            f"Could not create the scratch database {name!r}: {exc}"
        ) from exc


def _drop(connection: Any, name: str) -> None:
    quoted = connection.ops.quote_name(name)
    try:
        try:
            with connection.creation._nodb_cursor() as cursor:
                cursor.execute(f"DROP DATABASE {quoted}")
        except Exception:
            if connection.vendor != "postgresql":
                raise
            # A connection left open elsewhere (another thread, a pool):
            # PostgreSQL 13+ can end it.
            with connection.creation._nodb_cursor() as cursor:
                cursor.execute(f"DROP DATABASE {quoted} WITH (FORCE)")
    except Exception as exc:
        raise ScratchDatabaseError(
            f"Could not drop the scratch database {name!r}, please drop it"
            f" by hand: {exc}"
        ) from exc


def _exit_on_termination() -> Callable[[], None]:
    """
    Turn SIGTERM/SIGHUP into SystemExit while a scratch database exists, so
    the cleanup above still runs when the process is asked to stop (only
    SIGKILL and the like can leave one behind; see list_leftovers()).
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    previous: dict[int, Any] = {}

    def handler(signum: int, frame: Any) -> None:
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.signal(signum, handler)

    def restore() -> None:
        for signum, old in previous.items():
            signal.signal(signum, old)

    return restore


def list_leftovers(alias: str = "default") -> list[str]:
    """Scratch databases (or SQLite directories) left by killed runs."""
    connection = connections[alias]
    if connection.vendor == "sqlite":
        return sorted(str(p) for p in Path(tempfile.gettempdir()).glob(f"{PREFIX}*"))
    if connection.vendor not in _CREATE_DATABASE_VENDORS:
        raise ScratchDatabaseError(
            f"Scratch databases aren't supported for the {connection.vendor!r} backend."
        )
    query = (
        "SELECT datname FROM pg_database WHERE datname LIKE %s"
        if connection.vendor == "postgresql"
        else "SELECT schema_name FROM information_schema.schemata"
        " WHERE schema_name LIKE %s"
    )
    creation: Any = connection.creation
    with creation._nodb_cursor() as cursor:
        cursor.execute(query, [PREFIX.replace("_", "\\_") + "%"])
        return sorted(row[0] for row in cursor.fetchall())


def drop_leftover(name: str, alias: str = "default") -> None:
    """Drop one scratch database (or SQLite directory) by name."""
    connection = connections[alias]
    if connection.vendor == "sqlite":
        path = Path(name)
        if not path.name.startswith(PREFIX) or path.parent != Path(
            tempfile.gettempdir()
        ):
            raise ScratchDatabaseError(f"{name!r} is not a scratch directory.")
        shutil.rmtree(path, ignore_errors=True)
        return
    if not name.startswith(PREFIX) or name == connection.settings_dict["NAME"]:
        raise ScratchDatabaseError(f"{name!r} is not a scratch database.")
    _drop(connection, name)
