import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

DEBUG = os.environ.get("DEBUG", "") == "1"

SECRET_KEY = ")w%-67b9lurhzs*o2ow(e=n_^(n2!0_f*2+g+1*9tcn6_k58(f"

# Dangerous: disable host header validation
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "htmx",
    "example.core",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "htmx.middlewares.HtmxMiddleware",
]

ROOT_URLCONF = "example.urls"

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.path.join(BASE_DIR.parent, 'db.sqlite3'),
    }
}

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ]
        },
    }
]

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
