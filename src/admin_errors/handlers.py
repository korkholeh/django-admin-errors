"""The universal capture entry point: Django's own `django.request` logger already logs 5xx with
`exc_info`, so unhandled view exceptions are captured through this handler without any middleware.

Nothing here may be imported eagerly beyond the standard library. `django.setup()` runs
`configure_logging()` *before* `apps.populate()`, so a project that names this handler in its own
`LOGGING` dict — which `admin_errors.W002` tells it to do whenever `propagate` is `False` on
`django.request` — has `dictConfig` construct this class while the app registry is still empty.
`admin_errors.capture` reaches `admin_errors.models`, so importing it at module scope turned that
documented configuration into `ValueError: Unable to configure handler 'admin_errors'` at startup.
"""

from __future__ import annotations

import logging


class AdminErrorsHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            from admin_errors import capture
        except Exception:
            # A record logged before the app registry is ready — during `django.setup()` itself,
            # or from a module imported by `settings.py`. There is no database to write it to yet,
            # and a logging handler that raises would take down whatever was being logged.
            return
        # capture.capture_record() never raises: no self.handleError(), which would print to
        # stderr on a request that is already failing.
        capture.capture_record(record)
