"""The universal capture entry point: Django's own `django.request` logger already logs 5xx with
`exc_info`, so unhandled view exceptions are captured through this handler without any middleware.
"""

from __future__ import annotations

import logging

from admin_errors import capture


class AdminErrorsHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        # capture.capture_record() never raises: no self.handleError(), which would print to
        # stderr on a request that is already failing.
        capture.capture_record(record)
