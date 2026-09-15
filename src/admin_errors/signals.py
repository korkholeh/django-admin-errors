"""Public signals (spec section 10) plus the `got_request_exception` marker receiver.

`send_safely()` wraps Django's own `Signal.send_robust` — it already isolates each receiver from
the others' exceptions — and additionally counts the returned exceptions into
`writer.stats.receiver_errors` and reports the first one per exception type through
`capture._log_internal_once`, matching the "capture must never raise" contract for receivers too.

`send_robust` itself logs a raising receiver through the stdlib `django.dispatch` logger at ERROR.
That logger is not `admin_errors.*` and propagates to the root logger like any other, so without
holding the recursion guard for the duration of the dispatch, a raising receiver would have its own
`send_robust` failure log re-enter `AdminErrorsHandler` and get captured as a brand-new issue —
recursion through Django's own logging, not through admin_errors' code.
"""

from __future__ import annotations

import sys
from typing import Any

from django.dispatch import Signal

issue_created = Signal()  # issue, event (may be None for a count-only first item)
issue_regressed = Signal()  # issue, event
issue_status_changed = Signal()  # issue, old_status, new_status, user (fired from Phase 8 onward)


def send_safely(signal: Signal, **kwargs: Any) -> None:
    if not signal.has_listeners():
        return
    from admin_errors import capture, writer

    previously_active = getattr(capture._recursion_guard, "active", False)
    capture._recursion_guard.active = True
    try:
        for _receiver, response in signal.send_robust(sender=None, **kwargs):
            if isinstance(response, Exception):
                writer.stats.receiver_errors += 1
                capture._log_internal_once(f"signal_receiver.{type(response).__name__}", response)
    finally:
        capture._recursion_guard.active = previously_active


def mark_request_on_exception(sender: Any = None, *, request: Any = None, **kwargs: Any) -> None:
    exc = sys.exc_info()[1]
    if exc is None or request is None:
        return
    try:
        exc.__admin_errors_request__ = request
    except BaseException:
        pass
