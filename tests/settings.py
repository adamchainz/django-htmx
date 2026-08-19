from __future__ import annotations

from typing import Any

SECRET_KEY = "NOTASECRET"

ALLOWED_HOSTS: list[str] = []

DATABASES: dict[str, dict[str, Any]] = {}

INSTALLED_APPS = [
    "django_mcpz",
]

MCPZ_TOKEN = "test-mcpz-token"

MIDDLEWARE: list[str] = []

ROOT_URLCONF = "tests.urls"

USE_TZ = True
