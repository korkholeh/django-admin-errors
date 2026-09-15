"""Django system checks for admin_errors.

Registered from `AdminErrorsConfig.ready()`, not via a module-level `@register` decorator, so
importing this module has no side effect. Checks must never query the database.
"""

import sqlite3
from typing import Any

from django.conf import settings as django_settings
from django.core.checks import CheckMessage, Error, Warning

from admin_errors.conf import settings

W001_ID = "admin_errors.W001"
E002_ID = "admin_errors.E002"


def check_settings_keys(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    return [
        Warning(
            f"Unknown ADMIN_ERRORS setting {key!r}. See docs/spec.md section 5 for valid keys.",
            id=W001_ID,
        )
        for key in settings.unknown_keys()
    ]


def check_sqlite_version(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    alias = settings.DATABASE
    database = django_settings.DATABASES.get(alias)
    if database is None or not database.get("ENGINE", "").endswith("sqlite3"):
        return []
    if sqlite3.sqlite_version_info < (3, 9):
        return [
            Error(
                f"SQLite {sqlite3.sqlite_version} has no JSON1 support (requires >= 3.9).",
                id=E002_ID,
            )
        ]
    return []
