"""Settings proxy for the `ADMIN_ERRORS` dict.

Always read settings through `admin_errors.conf.settings`, never
`django.conf.settings.ADMIN_ERRORS` directly: the proxy caches the merged dict and clears the cache
on Django's `setting_changed` signal, which is what makes `override_settings` work in tests.

Mutable defaults (`IGNORE_LOGGERS`, `IGNORE_EXCEPTIONS`, `IN_APP_EXCLUDE`, `NOTIFY_ON`) are shared
list objects. Callers must not mutate them.
"""

from typing import Any

from django.conf import settings as django_settings
from django.test.signals import setting_changed

DEFAULTS: dict[str, object] = {
    "ENABLED": True,
    "DATABASE": "default",
    "TRANSPORT": "thread",
    "CAPTURE_LEVEL": "ERROR",
    "AUTO_INSTALL_LOGGING_HANDLER": True,
    "CAPTURE_IN_DEBUG": True,
    "IGNORE_LOGGERS": ["django.security.DisallowedHost"],
    "IGNORE_EXCEPTIONS": [
        "django.http.Http404",
        "django.core.exceptions.PermissionDenied",
    ],
    "IGNORE_HTTP_STATUS_BELOW": 500,
    "BEFORE_SEND": None,
    "IN_APP_INCLUDE": None,
    "IN_APP_EXCLUDE": ["site-packages", "dist-packages", "/lib/python"],
    "CAPTURE_LOCALS": True,
    "CAPTURE_REQUEST_BODY": False,
    "MAX_BODY_BYTES": 4096,
    "MAX_VAR_REPR_LENGTH": 200,
    "MAX_FRAMES": 50,
    "MAX_PAYLOAD_BYTES": 65536,
    "EVENTS_PER_ISSUE": 20,
    "EVENT_SAMPLE_PER_HOUR": 10,
    "NEW_ISSUES_PER_MINUTE": 50,
    "QUEUE_MAXSIZE": 1000,
    "FLUSH_INTERVAL_SECONDS": 1.0,
    "FLUSH_BATCH_SIZE": 200,
    "EVENT_RETENTION_DAYS": 30,
    "DAILY_COUNT_RETENTION_DAYS": 90,
    "RESOLVED_ISSUE_TTL_DAYS": 14,
    "OPEN_ISSUE_TTL_DAYS": 90,
    "IGNORED_ISSUE_TTL_DAYS": None,
    "MAX_ISSUES": 5000,
    "CLEANUP": "opportunistic",
    "CLEANUP_INTERVAL_SECONDS": 3600,
    "SQLITE_VACUUM": "incremental",
    "NOTIFY_BACKEND": "admin_errors.notifications.EmailNotifier",
    "NOTIFY_RECIPIENTS": None,
    "NOTIFY_THROTTLE_SECONDS": 3600,
    "NOTIFY_ON": ["created", "regressed"],
    "NOTIFY_BASE_URL": "",
    "ADMIN_SITE": None,
    "INTERNAL_LOGGING": False,
}


class Settings:
    """Merged view of `DEFAULTS` and the host's `ADMIN_ERRORS` dict, cached until `reset()`."""

    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    def _resolved(self) -> dict[str, Any]:
        if self._cache is None:
            host = getattr(django_settings, "ADMIN_ERRORS", {})
            self._cache = {**DEFAULTS, **host}
        return self._cache

    def __getattr__(self, name: str) -> Any:
        if name not in DEFAULTS:
            raise AttributeError(f"admin_errors.conf.settings has no setting {name!r}")
        return self._resolved()[name]

    def as_dict(self) -> dict[str, Any]:
        return dict(self._resolved())

    def unknown_keys(self) -> list[str]:
        host = getattr(django_settings, "ADMIN_ERRORS", {})
        return sorted(key for key in host if key not in DEFAULTS)

    def reset(self) -> None:
        self._cache = None


settings = Settings()


def _on_setting_changed(*, sender: Any = None, **kwargs: Any) -> None:
    settings.reset()


setting_changed.connect(_on_setting_changed)
