from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings
from django.db import connections

from tests.project import Project


@pytest.fixture(autouse=True)
def no_bytecode(monkeypatch: pytest.MonkeyPatch) -> None:
    # Files are rewritten within the same second as they are first imported.
    monkeypatch.setattr(sys, "dont_write_bytecode", True)


@pytest.fixture
def project(tmp_path: Path, django_db_blocker: Any) -> Iterator[Project]:
    p = Project(tmp_path, django_db_blocker)
    try:
        yield p
    finally:
        p.close()


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


@pytest.fixture
def pg(django_db_blocker: Any) -> Iterator[str]:
    """The PostgreSQL server from compose.yaml."""
    with server_alias("pg", django_db_blocker) as alias:
        yield alias


@pytest.fixture(params=["mysql", "mariadb"])
def my(request: Any, django_db_blocker: Any) -> Iterator[str]:
    """The MySQL and the MariaDB server from compose.yaml."""
    with server_alias(request.param, django_db_blocker) as alias:
        yield alias
