"""Optional database router that pins the three admin_errors models to `conf.DATABASE`.

Not installed by default; a host that wants a dedicated alias (spec section 11.3) adds this class
to `DATABASE_ROUTERS`. The router never opines about apps other than `admin_errors`, so it is safe
to add alongside a host's own routers.
"""

from __future__ import annotations

from typing import Any

from admin_errors.conf import settings as conf

APP_LABEL = "admin_errors"


class AdminErrorsRouter:
    def db_for_read(self, model: type, **hints: Any) -> str | None:
        if model._meta.app_label == APP_LABEL:
            return conf.DATABASE
        return None

    def db_for_write(self, model: type, **hints: Any) -> str | None:
        if model._meta.app_label == APP_LABEL:
            return conf.DATABASE
        return None

    def allow_relation(self, obj1: Any, obj2: Any, **hints: Any) -> bool | None:
        if obj1._meta.app_label == APP_LABEL or obj2._meta.app_label == APP_LABEL:
            return True
        return None

    def allow_migrate(
        self, db: str, app_label: str, model_name: str | None = None, **hints: Any
    ) -> bool | None:
        if app_label == APP_LABEL:
            return db == conf.DATABASE
        return None
