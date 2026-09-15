"""Minimal Django host used by the test suite (`uv run pytest -q`)."""

import os
from urllib.parse import unquote, urlsplit

DEFAULT_PG_URL = "postgres://postgres:postgres@localhost:5432/admin_errors_test"


def database_from_url(url: str) -> dict[str, object]:
    """Parse a `postgres://` DSN into a Django `DATABASES` entry.

    Raises `ValueError` on any non-postgres scheme instead of building a half-config.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"unsupported database URL scheme: {parts.scheme!r}")
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(parts.path.lstrip("/")),
        "USER": unquote(parts.username) if parts.username else "",
        "PASSWORD": unquote(parts.password) if parts.password else "",
        "HOST": parts.hostname or "",
        "PORT": str(parts.port) if parts.port else "",
    }


if os.environ.get("DJANGO_DB") == "postgres":
    DATABASES = {
        "default": database_from_url(os.environ.get("ADMIN_ERRORS_TEST_PG_URL", DEFAULT_PG_URL))
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.admin",
    "admin_errors",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

ROOT_URLCONF = "tests.urls"

USE_TZ = True
TIME_ZONE = "UTC"
SECRET_KEY = "test-secret-key-not-for-production"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

ADMIN_ERRORS = {"TRANSPORT": "sync"}
