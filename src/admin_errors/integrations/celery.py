"""Celery `task_failure` integration (spec section 6.4/9.3).

Only imported when Celery is actually present — `apps.ready()` gates the import behind
`importlib.util.find_spec("celery") is not None` rather than a bare `try/except ImportError`, so a
genuine bug in this module surfaces instead of looking like "Celery is not installed".

Double capture is already handled upstream: Celery's own `celery.app.trace` logs the same failure
at ERROR *after* the `task_failure` signal fires, and `capture._mark_captured_once()` makes this
receiver's earlier call win.
"""

from __future__ import annotations

from typing import Any

from celery.signals import task_failure

from admin_errors import capture

_DISPATCH_UID = "admin_errors.task_failure"


def on_task_failure(
    sender: Any = None,
    task_id: str | None = None,
    exception: BaseException | None = None,
    args: Any = None,
    kwargs: Any = None,
    traceback: Any = None,
    einfo: Any = None,
    **extra: Any,
) -> None:
    if exception is None:
        return
    capture.capture_exception_info(
        type(exception),
        exception,
        traceback or exception.__traceback__,
        celery={
            "task": getattr(sender, "name", ""),
            "task_id": task_id,
            "args": repr(args)[:500],
            "kwargs": repr(kwargs)[:500],
        },
    )


def connect() -> None:
    task_failure.connect(on_task_failure, dispatch_uid=_DISPATCH_UID)
