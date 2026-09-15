"""Django system checks for admin_errors.

Registered from `AdminErrorsConfig.ready()`, not via a module-level `@register` decorator, so
importing this module has no side effect. Checks must never query the database.
"""

import sqlite3
from typing import Any

from django.conf import settings as django_settings
from django.core.checks import CheckMessage, Error, Warning
from django.utils.module_loading import import_string

from admin_errors.conf import settings
from admin_errors.handlers import AdminErrorsHandler

W001_ID = "admin_errors.W001"
E001_ID = "admin_errors.E001"
E002_ID = "admin_errors.E002"
W002_ID = "admin_errors.W002"

_PROPAGATION_SENSITIVE_LOGGERS = ("django.request", "django")


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


def check_database_alias(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    alias = settings.DATABASE
    if alias in django_settings.DATABASES:
        return []
    return [
        Error(
            f"ADMIN_ERRORS['DATABASE'] is {alias!r}, which is not a key of settings.DATABASES.",
            id=E001_ID,
        )
    ]


def _logger_has_admin_errors_handler(logger_config: dict[str, Any]) -> bool:
    handlers = logger_config.get("handlers") or []
    handler_configs = (django_settings.LOGGING or {}).get("handlers", {})
    for handler_name in handlers:
        handler_config = handler_configs.get(handler_name, {})
        handler_class = handler_config.get("class") or handler_config.get("()", "")
        if not isinstance(handler_class, str):
            continue
        if handler_class in (
            "admin_errors.handlers.AdminErrorsHandler",
            "admin_errors.handlers:AdminErrorsHandler",
        ):
            return True
        if not handler_class:
            continue
        try:
            cls = import_string(handler_class)
        except (ImportError, ValueError):
            continue
        if isinstance(cls, type) and issubclass(cls, AdminErrorsHandler):
            return True
    return False


def check_logging_propagation(app_configs: Any = None, **kwargs: Any) -> list[CheckMessage]:
    logging_config = getattr(django_settings, "LOGGING", None) or {}
    loggers = logging_config.get("loggers", {})
    messages: list[CheckMessage] = []
    for logger_name in _PROPAGATION_SENSITIVE_LOGGERS:
        logger_config = loggers.get(logger_name)
        if not logger_config or logger_config.get("propagate", True) is not False:
            continue
        if _logger_has_admin_errors_handler(logger_config):
            continue
        messages.append(
            Warning(
                f"Logger {logger_name!r} has propagate=False and no admin_errors handler, so its "
                "records never reach admin_errors. Add "
                "'admin_errors.handlers.AdminErrorsHandler' to its own 'handlers' list.",
                id=W002_ID,
            )
        )
    return messages
