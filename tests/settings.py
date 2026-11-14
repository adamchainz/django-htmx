from __future__ import annotations

from typing import Any

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
