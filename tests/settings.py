from __future__ import annotations

from typing import Any

import pymysql

# Django wants mysqlclient 2.2.1 or later: PyMySQL stands in for it.
pymysql.version_info = (2, 2, 1, "final", 0)
pymysql.install_as_MySQLdb()

SECRET_KEY = "NOTASECRET"

DATABASES: dict[str, dict[str, Any]] = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

INSTALLED_APPS = [
    "django_clean_migrations",
]

USE_TZ = True
