"""The documented public surface (ADR 0008). Internals may move; this module does not."""

from __future__ import annotations

import sys
from typing import Any

from django.http import HttpRequest

from admin_errors import capture


def capture_exception(
    exc: BaseException | None = None,
    *,
    request: HttpRequest | None = None,
    extra: dict[str, Any] | None = None,
    fingerprint: object = None,
    level: str = "error",
) -> str | None:
    """Capture `exc`, or the currently handled exception when `exc` is `None`.

    Returns the fingerprint, or `None` when nothing was captured (no active exception, or dropped).
    """
    if exc is None:
        exc_type, exc_value, tb = sys.exc_info()
    else:
        exc_type, exc_value, tb = type(exc), exc, exc.__traceback__
    if exc_value is None:
        return None
    return capture.capture_exception_info(
        exc_type,
        exc_value,
        tb,
        request=request,
        extra=extra,
        fingerprint=fingerprint,
        level=level,
    )


def capture_message(
    message: str,
    *,
    level: str = "error",
    request: HttpRequest | None = None,
    extra: dict[str, Any] | None = None,
    fingerprint: object = None,
) -> str | None:
    return capture.capture_message_info(
        message, level=level, request=request, extra=extra, fingerprint=fingerprint
    )


def flush(timeout: float | None = 2.0) -> None:
    """Block until the writer queue is drained.

    No-op under `TRANSPORT="sync"` (every capture is already written by the time it returns). The
    thread transport's writer is wired in a later phase.
    """
    return None
