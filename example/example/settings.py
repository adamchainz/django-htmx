from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Hide development server warning
# https://docs.djangoproject.com/en/stable/ref/django-admin/#envvar-DJANGO_RUNSERVER_HIDE_WARNING
os.environ["DJANGO_RUNSERVER_HIDE_WARNING"] = "true"

BASE_DIR = Path(__file__).parent.parent

DEBUG = True

SECRET_KEY = "django-insecure-example-project-only"

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]

# The bearer token MCP clients must send. Hard-coded for this example
# project only—in a real project, load it from the environment or another
# secret store, like SECRET_KEY.
MCPZ_TOKEN = "easy-peasy-example-token"

INSTALLED_APPS = [
    "diner",
    "django_mcpz",
]

MIDDLEWARE: list[str] = []

ROOT_URLCONF = "example.urls"

DATABASES: dict[str, dict[str, Any]] = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

USE_TZ = True

WSGI_APPLICATION = "example.wsgi.application"
