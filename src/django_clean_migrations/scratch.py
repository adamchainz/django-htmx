"""
Throwaway databases for running the migration history for real.

The connection for the chosen alias is pointed at a freshly created, empty
database for the duration (as Django's test runner does), so data migrations
that use the default manager without ``using()`` hit the scratch database
rather than the real one. Other aliases are left alone.
"""

from __future__ import annotations

import secrets
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import connections
from django.db.backends.base.base import BaseDatabaseWrapper

PREFIX = "cleanmig_"
_CREATE_DATABASE_VENDORS = frozenset({"postgresql", "mysql"})


class ScratchDatabaseError(Exception):
    pass


def _scratch_name(real_name: str, max_length: int) -> str:
    # A distinct prefix (not Django's "test_") so a test database kept with
    # --keepdb is never clobbered; a random part so runs never collide.
    name = f"{PREFIX}{secrets.token_hex(4)}_{real_name}"
    return name[:max_length]


@contextmanager
def scratch_database(alias: str = "default") -> Iterator[BaseDatabaseWrapper]:
    """
    Create an empty database, point ``connections[alias]`` at it, yield the
    connection, and always destroy the database and restore the settings
    afterwards, even on KeyboardInterrupt.
    """
    if alias not in connections:
        raise ScratchDatabaseError(f"There is no database alias {alias!r}.")
    connection = connections[alias]
    vendor = connection.vendor
    settings_dict = connection.settings_dict
    real_name = settings_dict["NAME"]
    configured = settings.DATABASES.get(alias)
    configured_name = configured.get("NAME") if configured is not None else None

    temp_dir: str | None = None
    created: str | None = None
    if vendor == "sqlite":
        temp_dir = tempfile.mkdtemp(prefix=PREFIX)
        scratch = str(Path(temp_dir) / "scratch.sqlite3")
    elif vendor in _CREATE_DATABASE_VENDORS:
        max_length = getattr(connection.ops, "max_name_length", lambda: 63)() or 63
        scratch = _scratch_name(str(real_name or ""), min(int(max_length), 63))
    else:
        raise ScratchDatabaseError(
            f"Scratch databases aren't supported for the {vendor!r} backend."
        )
    if scratch == real_name:
        raise ScratchDatabaseError(
            f"Refusing to use {scratch!r}: it is the real database's name."
        )

    connection.close()
    try:
        if vendor != "sqlite":
            _create(connection, scratch)
            created = scratch
        settings_dict["NAME"] = scratch
        if configured is not None:
            configured["NAME"] = scratch
        yield connection
    finally:
        connection.close()
        close_pool = getattr(connection, "close_pool", None)
        if close_pool is not None:
            close_pool()
        settings_dict["NAME"] = real_name
        if configured is not None:
            configured["NAME"] = configured_name
        try:
            if created is not None:
                _drop(connection, created)
        finally:
            if temp_dir is not None:
                shutil.rmtree(temp_dir, ignore_errors=True)


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
    try:
        with connection.creation._nodb_cursor() as cursor:
            cursor.execute(f"DROP DATABASE {connection.ops.quote_name(name)}")
    except Exception as exc:
        raise ScratchDatabaseError(
            f"Could not drop the scratch database {name!r}, please drop it"
            f" by hand: {exc}"
        ) from exc
