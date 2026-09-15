"""Minimal Django host used by the test suite (`uv run pytest -q`)."""

import os
import tempfile
from urllib.parse import unquote, urlsplit

import django

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
    # `NAME` is `:memory:` for non-test invocations (`django check`, `makemigrations --check`):
    # fast, no file left behind. Django replaces it with `TEST["NAME"]` for the whole test run,
    # so every test in this suite actually runs against a file-backed database, not `:memory:` —
    # an in-memory SQLite database is one connection's private state, so two real threads opening
    # "the same" `:memory:` database (the `test_two_threads_...` concurrency case) would not
    # actually share data. The filename is qualified with the tox env name so parallel tox runs
    # (`tox -p`) do not race two interpreters over the same file.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
            "TEST": {
                "NAME": os.path.join(
                    tempfile.gettempdir(),
                    f"admin_errors_test_{os.environ.get('TOX_ENV_NAME', 'local')}.sqlite3",
                ),
            },
            # SQLite's default DEFERRED transaction lets two connections each take a SHARED read
            # lock and then deadlock trying to upgrade to a write lock, raised immediately as
            # "database is locked" rather than retried under the busy timeout. IMMEDIATE takes the
            # write lock upfront, so a genuine second writer blocks-then-retries as intended by
            # `test_two_threads_storing_one_new_fingerprint_create_one_issue`. `transaction_mode`
            # is a Django 5.1+ OPTIONS key (unknown to 4.2's sqlite3 backend, which would pass it
            # straight to `sqlite3.connect()` and raise `TypeError`); on 4.2 the same effect comes
            # from `tests.sqlite_immediate`, a backend that overrides the private method the base
            # backend uses to issue `BEGIN` (see DECISIONS.md).
            "OPTIONS": {"transaction_mode": "IMMEDIATE"} if django.VERSION >= (5, 1) else {},
        }
    }
    if django.VERSION < (5, 1):
        DATABASES["default"]["ENGINE"] = "tests.sqlite_immediate"

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
