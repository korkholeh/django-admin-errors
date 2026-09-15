"""This phase only holds the `got_request_exception` marker receiver, connected from
`AppConfig.ready()`. It never captures by itself: it marks the exception with its request so the
logging handler can pick it up even when the project's logging config prevents the `django.request`
record from reaching the root logger.

`issue_created` / `issue_regressed` / `issue_status_changed` land in a later phase.
"""

from __future__ import annotations

import sys
from typing import Any


def mark_request_on_exception(sender: Any = None, *, request: Any = None, **kwargs: Any) -> None:
    exc = sys.exc_info()[1]
    if exc is None or request is None:
        return
    try:
        exc.__admin_errors_request__ = request
    except BaseException:
        pass
