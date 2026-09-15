"""Builds the JSON-serialisable event payload (spec section 6.4).

The only module that touches untrusted host data: request headers, POST bodies, cookies, local
variables. Scrubbing is always delegated to Django's own
`django.views.debug.get_exception_reporter_filter` — never hand-rolled — so `@sensitive_variables`,
`@sensitive_post_parameters` and `DEFAULT_EXCEPTION_REPORTER_FILTER` are respected automatically.

Every sub-block is built in its own `try/except BaseException`: a failure while reading the request
must not cost the traceback frames, and vice versa. Never calls
`ExceptionReporter.get_traceback_data()` (it also collects the whole settings module and is slower);
only `get_exception_traceback_frames()`.
"""

from __future__ import annotations

import contextvars
import logging
import os
import platform
import re
from collections.abc import Sequence
from datetime import datetime
from types import TracebackType
from typing import Any

import django
from django.conf import settings as django_settings
from django.http import HttpRequest
from django.views.debug import ExceptionReporter, get_exception_reporter_filter

from admin_errors.conf import settings as conf

_UNREPRESENTABLE_FALLBACK = "<unrepresentable>"

# `SafeExceptionReporterFilter.hidden_settings` only gained "AUTH" in its pattern between Django
# 4.2 and 5.x; CLAUDE.md's supported range starts at 4.2. Applied on top of `cleanse_setting`'s own
# (version-dependent) redaction so `Authorization`/`Proxy-Authorization` are always scrubbed,
# regardless of which Django version built `filter_`.
_ALWAYS_HIDDEN_META_KEYS = re.compile("AUTH", re.IGNORECASE)

# Standard `logging.LogRecord` attributes
# (docs.python.org/3/library/logging.html#logrecord-attributes)
# plus "message"/"asctime" (added by a Formatter) and "taskName" (3.12+). Anything else on a record
# came from `extra=` and belongs in the payload's "extra" block.
_STANDARD_LOG_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "message",
        "asctime",
        "taskName",
        # Consumed elsewhere in the capture pipeline, never surfaced as "extra".
        "request",
        "fingerprint",
        "status_code",
    }
)

_current_request: contextvars.ContextVar[HttpRequest | None] = contextvars.ContextVar(
    "admin_errors_current_request", default=None
)


def get_current_request() -> HttpRequest | None:
    """The request stored by `RequestContextMiddleware`, or `None` outside of one."""
    return _current_request.get()


def safe_repr(value: object, *, limit: int) -> str:
    """`repr()` that can never raise. Falls back to a fixed string, truncates to `limit`."""
    try:
        text = repr(value)
    except BaseException:
        try:
            name = type(value).__name__
        except BaseException:
            return _UNREPRESENTABLE_FALLBACK
        return f"<unrepresentable {name}>"
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def is_in_app(filename: str) -> bool:
    """`IN_APP_EXCLUDE` substrings win first, then `IN_APP_INCLUDE` prefixes.

    `IN_APP_INCLUDE=None` resolves to `[str(settings.BASE_DIR)]` when `BASE_DIR` is defined. When
    no prefix can be resolved at all, every non-excluded frame counts as in-app (a library
    installed with no `BASE_DIR` and no explicit include would otherwise never have an in-app
    culprit).
    """
    exclude = conf.IN_APP_EXCLUDE
    if any(marker in filename for marker in exclude):
        return False
    include = conf.IN_APP_INCLUDE
    if include is None:
        base_dir = getattr(django_settings, "BASE_DIR", None)
        include = [str(base_dir)] if base_dir else []
    if not include:
        return True
    return any(filename.startswith(prefix) for prefix in include)


def _frame_culprit(frame: Any) -> str:
    module = frame.f_globals.get("__name__", "?")
    return f"{module}.{frame.f_code.co_name}"


def select_culprit(tb: TracebackType | None) -> str:
    """Innermost in-app frame of `tb`, else the innermost frame overall. No `ExceptionReporter`."""
    frames = []
    while tb is not None:
        frames.append(tb.tb_frame)
        tb = tb.tb_next
    if not frames:
        return ""
    for frame in reversed(frames):
        try:
            if is_in_app(frame.f_code.co_filename):
                return _frame_culprit(frame)
        except BaseException:
            continue
    return _frame_culprit(frames[-1])


def build_frames(
    request: HttpRequest | None,
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    tb: TracebackType | None,
) -> list[dict[str, Any]]:
    """Frames of one exception's own traceback, innermost last.

    Never the whole `__cause__` chain.
    """
    if exc_value is None:
        return []
    try:
        reporter = ExceptionReporter(request, exc_type, exc_value, tb)
        raw_frames = list(reporter.get_exception_traceback_frames(exc_value, tb))
    except BaseException:
        return []

    capture_locals = conf.CAPTURE_LOCALS
    var_limit = conf.MAX_VAR_REPR_LENGTH
    frames: list[dict[str, Any]] = []
    for raw in raw_frames:
        raw_tb = raw.get("tb")
        if raw_tb is None:
            # Separator entry emitted only when an exception in the chain has no traceback at all.
            continue
        try:
            filename = raw.get("filename", "")
            frame: dict[str, Any] = {
                "filename": filename,
                "lineno": raw.get("lineno"),
                "function": raw.get("function"),
                "module": raw_tb.tb_frame.f_globals.get("__name__"),
                "in_app": is_in_app(filename),
                "pre_context": raw.get("pre_context") or [],
                "context_line": raw.get("context_line") or "",
                "post_context": raw.get("post_context") or [],
            }
            if capture_locals:
                frame["vars"] = {
                    name: safe_repr(value, limit=var_limit) for name, value in raw.get("vars", [])
                }
        except BaseException:
            continue
        frames.append(frame)

    max_frames = conf.MAX_FRAMES
    if max_frames and len(frames) > max_frames:
        frames = frames[-max_frames:]
    return frames


def _exc_message(exc_value: BaseException) -> str:
    try:
        return str(exc_value)
    except BaseException:
        return safe_repr(exc_value, limit=conf.MAX_VAR_REPR_LENGTH)


def build_exception_block(
    request: HttpRequest | None,
    exc_type: type[BaseException],
    exc_value: BaseException,
    tb: TracebackType | None,
) -> dict[str, Any]:
    """Outermost exception's `type`/`module`/`value` plus `chain` (outermost first).

    `chain[i]["cause"]` is `True` when that exception carries an explicit `raise ... from ...` link
    to the next (inner) entry, `False` for an implicit `__context__` link or when there is no
    further link.
    """
    nodes: list[tuple[BaseException, TracebackType | None, bool]] = []
    seen: set[int] = set()
    current: BaseException | None = exc_value
    current_tb = tb
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        cause = current.__cause__
        explicit = cause is not None
        if cause is None and not getattr(current, "__suppress_context__", False):
            cause = current.__context__
        if cause is None or id(cause) in seen:
            nodes.append((current, current_tb, False))
            break
        nodes.append((current, current_tb, explicit))
        current = cause
        current_tb = cause.__traceback__

    chain = []
    for exc, exc_tb, cause_flag in nodes:
        chain.append(
            {
                "type": type(exc).__qualname__,
                "module": type(exc).__module__,
                "value": _exc_message(exc),
                "cause": cause_flag,
                "frames": build_frames(request, type(exc), exc, exc_tb),
            }
        )

    outer = chain[0]
    return {
        "type": outer["type"],
        "module": outer["module"],
        "value": outer["value"],
        "chain": chain,
    }


def _header_case(meta_key: str) -> str:
    if meta_key.startswith("HTTP_"):
        meta_key = meta_key[5:]
    return "-".join(part.capitalize() for part in meta_key.split("_"))


def _querydict_to_dict(query_dict: Any) -> dict[str, Any]:
    """Like `QueryDict.dict()`, but keeps repeated keys as a list instead of only the last value."""
    return {key: values if len(values) > 1 else values[0] for key, values in query_dict.lists()}


def build_request_block(request: HttpRequest | None) -> dict[str, Any] | None:
    """Section 7.2.6: entirely delegated to `get_exception_reporter_filter(request)`."""
    if request is None:
        return None
    try:
        filter_ = get_exception_reporter_filter(request)
    except BaseException:
        return None

    block: dict[str, Any] = {}

    try:
        block["method"] = request.method
    except BaseException:
        block["method"] = None

    try:
        block["path"] = request.path
    except BaseException:
        block["path"] = None

    try:
        block["url"] = request.build_absolute_uri()
    except BaseException:
        # Covers DisallowedHost and any other failure while resolving the host.
        block["url"] = block["path"]

    try:
        block["query"] = _querydict_to_dict(request.GET)
    except BaseException:
        block["query"] = {}

    try:
        post = filter_.get_post_parameters(request)
        post_dict = _querydict_to_dict(post) if hasattr(post, "lists") else dict(post)
        # `get_post_parameters` only cleanses names listed by `@sensitive_post_parameters`; an
        # undecorated field named e.g. "password" would otherwise be stored verbatim. Cleanse by
        # key too, the same way `get_safe_request_meta`/`get_safe_cookies` already do below.
        block["post"] = {
            key: filter_.cleanse_setting(key, value) for key, value in post_dict.items()
        }
    except BaseException:
        block["post"] = {}

    try:
        meta = filter_.get_safe_request_meta(request)
    except BaseException:
        meta = {}
    headers: dict[str, str] = {}
    for key, value in meta.items():
        if key.startswith("HTTP_") or key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            if _ALWAYS_HIDDEN_META_KEYS.search(key):
                value = filter_.cleansed_substitute
            headers[_header_case(key)] = (
                value if isinstance(value, str) else safe_repr(value, limit=200)
            )
    block["headers"] = headers

    try:
        block["cookies"] = dict(filter_.get_safe_cookies(request))
    except BaseException:
        block["cookies"] = {}

    try:
        block["remote_addr"] = request.META.get("REMOTE_ADDR")
    except BaseException:
        block["remote_addr"] = None

    block["user"] = None
    try:
        user = request.user
        if getattr(user, "is_authenticated", False):
            block["user"] = {
                "id": user.pk,
                "username": str(user),
                "email": getattr(user, "email", ""),
            }
    except BaseException:
        block["user"] = None

    block["body"] = None
    if conf.CAPTURE_REQUEST_BODY:
        try:
            body = request.body[: conf.MAX_BODY_BYTES]
            block["body"] = body.decode("utf-8", errors="replace")
        except BaseException:
            block["body"] = None

    return block


def build_extra(record: logging.LogRecord | None) -> dict[str, str]:
    """`LogRecord` attributes outside the standard set, e.g. from `extra={...}`."""
    if record is None:
        return {}
    limit = conf.MAX_VAR_REPR_LENGTH
    extra: dict[str, str] = {}
    try:
        items = list(vars(record).items())
    except BaseException:
        return {}
    for key, value in items:
        if key in _STANDARD_LOG_RECORD_ATTRS or key.startswith("_"):
            continue
        extra[key] = safe_repr(value, limit=limit)
    return extra


def _server_block() -> dict[str, Any]:
    try:
        return {
            "hostname": platform.node(),
            "pid": os.getpid(),
            "python": platform.python_version(),
            "django": django.get_version(),
        }
    except BaseException:
        return {}


def build_payload(
    *,
    source: str,
    level: str,
    logger: str,
    message: str,
    timestamp: datetime,
    request: HttpRequest | None,
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    tb: TracebackType | None,
    record: logging.LogRecord | None,
    celery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assembles the spec section 6.4 document. Never settings, never environment."""
    payload: dict[str, Any] = {
        "v": 1,
        "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
        "level": level,
        "logger": logger,
        "message": message,
        "source": source,
    }

    if exc_value is not None:
        try:
            exception_block = build_exception_block(request, exc_type, exc_value, tb)
        except BaseException:
            exception_block = None
        if exception_block is not None:
            payload["exception"] = exception_block
            payload["frames"] = exception_block["chain"][0]["frames"]

    try:
        request_block = build_request_block(request)
    except BaseException:
        request_block = None
    if request_block is not None:
        payload["request"] = request_block

    if celery:
        payload["celery"] = celery

    try:
        extra = build_extra(record)
    except BaseException:
        extra = {}
    if extra:
        payload["extra"] = extra

    payload["server"] = _server_block()
    return payload


__all__: Sequence[str] = (
    "build_exception_block",
    "build_extra",
    "build_frames",
    "build_payload",
    "build_request_block",
    "get_current_request",
    "is_in_app",
    "safe_repr",
    "select_culprit",
)
