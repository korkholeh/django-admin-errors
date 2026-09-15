"""Dependency-free settings for the `package` tox env / CI job.

Used to run `django-admin check` and `migrate` against a clean venv containing only the built
wheel plus Django, proving the wheel is complete (templates, static, locale, migrations).
"""

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "package-check.sqlite3",
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

ROOT_URLCONF = "tests.package_settings"
urlpatterns: list = []

USE_TZ = True
TIME_ZONE = "UTC"
SECRET_KEY = "package-check-secret-key"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
