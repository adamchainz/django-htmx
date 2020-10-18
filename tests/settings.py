SECRET_KEY = "NOTASECRET"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "ATOMIC_REQUESTS": True,
    }
}

ALLOWED_HOSTS = []

INSTALLED_APPS = []

MIDDLEWARE = []
