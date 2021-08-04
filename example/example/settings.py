import os
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).parent

DEBUG = os.environ.get("DEBUG", "") == "1"

SECRET_KEY = ")w%-67b9lurhzs*o2ow(e=n_^(n2!0_f*2+g+1*9tcn6_k58(f"

# Dangerous: disable host header validation
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "example.core",
    "django_htmx",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.csrf.CsrfViewMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "example.urls"

DATABASES: Dict[str, Dict[str, Any]] = {}

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "example.core.context_processors.debug",
            ]
        },
    }
]

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
