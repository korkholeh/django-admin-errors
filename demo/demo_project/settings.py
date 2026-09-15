"""Demo Django project settings (spec section 13). Plain module-level constants — no
`django.setup()`, no side effects — so `tests/test_demo.py` can `import demo_project.settings` and
reuse the real values through `override_settings` instead of re-declaring them.

Never imports from `tests/`: the demo must work standalone, with no test package on the path.
"""

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent

DEMO_PG_URL = "postgres://postgres:postgres@localhost:5432/admin_errors_test"


def _database_from_url(url: str) -> dict[str, object]:
    """Parse a `postgres://` DSN into a Django `DATABASES` entry.

    Duplicated from `tests/settings.py::database_from_url` rather than imported: the demo must not
    depend on the test package (see DECISIONS.md p07-plan).
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


if os.environ.get("DEMO_DB") == "postgres":
    DATABASES = {"default": _database_from_url(os.environ.get("DEMO_PG_URL", DEMO_PG_URL))}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(BASE_DIR / "demo.sqlite3"),
        }
    }

# `runserver` only serves the admin's static files under `DEBUG=True`, and the technical 500 page
# is part of what the demo shows off; `CAPTURE_IN_DEBUG` defaults to `True` so capture itself still
# works either way (see DECISIONS.md p07-plan).
DEBUG = os.environ.get("DEMO_DEBUG", "1") != "0"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "admin_errors",
    "demo_app",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "admin_errors.middleware.RequestContextMiddleware",
]

ROOT_URLCONF = "demo_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "demo_project.wsgi.application"

USE_TZ = True
TIME_ZONE = "UTC"
LANGUAGE_CODE = "en-us"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "static/"

# Never a secret: the demo is never deployed.
SECRET_KEY = "demo-only-not-a-secret"

ADMINS = [("Demo Admin", "admin@example.com")]
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# spec section 13's exact demo profile; everything else stays on the shipped defaults so the demo
# also demonstrates them.
ADMIN_ERRORS = {
    "TRANSPORT": "thread",
    "CAPTURE_LEVEL": "WARNING",
    "EVENT_SAMPLE_PER_HOUR": 5,
    "EVENT_RETENTION_DAYS": 7,
    "MAX_ISSUES": 200,
    "INTERNAL_LOGGING": True,
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "demo_app": {"handlers": ["console"], "level": "INFO"},
        "django.request": {"handlers": ["console"], "level": "INFO"},
    },
}
