from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Hide development server warning
# https://docs.djangoproject.com/en/stable/ref/django-admin/#envvar-DJANGO_RUNSERVER_HIDE_WARNING
os.environ["DJANGO_RUNSERVER_HIDE_WARNING"] = "true"

BASE_DIR = Path(__file__).parent

DEBUG = True

SECRET_KEY = ")w%-67b9lurhzs*o2ow(e=n_^(n2!0_f*2+g+1*9tcn6_k58(f"

# Dangerous: disable host header validation
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "example",
    "django_htmx",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.csrf.CsrfViewMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "example.urls"

DATABASES: dict[str, dict[str, Any]] = {}

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "example.context_processors.debug",
            ]
        },
    }
]

USE_TZ = True


STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
