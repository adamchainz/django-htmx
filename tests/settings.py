from typing import Any, Dict, List

SECRET_KEY = "NOTASECRET"

ALLOWED_HOSTS: List[str] = []

DATABASES: Dict[str, Dict[str, Any]] = {}

INSTALLED_APPS = [
    "django_htmx",
]

MIDDLEWARE: List[str] = []

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "OPTIONS": {"context_processors": []},
    }
]

USE_TZ = True
