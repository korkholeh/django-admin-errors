"""The single chokepoint every entry point funnels through (spec section 7.2).

Nothing here may ever raise or recurse: the whole pipeline runs inside its own
`try/except BaseException` (re-raising `KeyboardInterrupt`/`SystemExit`), guarded by a thread-local
recursion flag. Admission and sampling (`NEW_ISSUES_PER_MINUTE`, `EVENT_SAMPLE_PER_HOUR`) are not
implemented yet — every non-dropped item gets a full payload and is dispatched synchronously; the
writer thread and its buckets land in a later phase.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import logging
import threading
from types import TracebackType
from typing import Any

from django.conf import settings as django_settings
from django.http import HttpRequest
from django.utils import timezone
from django.utils.module_loading import import_string

from admin_errors import context, fingerprint, storage
from admin_errors.conf import settings as conf
from admin_errors.models import Issue

_MISSING = object()

# `Issue.level` only accepts the four `Issue.Level` choices and is `max_length=10`. Log level
# names and the free-form `level=` argument of `api.capture_exception`/`capture_message` do not
# have that guarantee: a `CAPTURE_LEVEL` below the root logger's own level (see apps.py) makes
# "debug" records reachable for the first time, and a caller can pass any string. Anything unknown
# clamps to `Issue.Level.ERROR` rather than being written verbatim and failing `store_batch` on
# PostgreSQL (silently, inside the pipeline's own `try/except BaseException`).
_LEVEL_MAP = {
    "critical": Issue.Level.CRITICAL,
    "fatal": Issue.Level.CRITICAL,
    "error": Issue.Level.ERROR,
    "exception": Issue.Level.ERROR,
    "warning": Issue.Level.WARNING,
    "warn": Issue.Level.WARNING,
    "info": Issue.Level.INFO,
    "debug": Issue.Level.INFO,
    "notset": Issue.Level.INFO,
}


def _normalize_level(level: str) -> str:
    try:
        return _LEVEL_MAP.get(level.strip().lower(), Issue.Level.ERROR)
    except BaseException:
        return Issue.Level.ERROR


_recursion_guard = threading.local()
_internal_logged_types: set[str] = set()
_ignore_exception_types_cache: dict[str, type[BaseException] | None] = {}

_API_LOGGER = "admin_errors.api"


@dataclasses.dataclass(frozen=True)
class CapturedItem:
    fingerprint: str
    meta: dict[str, str]
    timestamp: datetime.datetime
    payload: dict[str, Any] | None


def _log_internal_once(key: str, exc: BaseException | None) -> None:
    if not conf.INTERNAL_LOGGING or key in _internal_logged_types:
        return
    _internal_logged_types.add(key)
    try:
        logging.getLogger("admin_errors.internal").warning(
            "admin_errors: capture pipeline error (%s)", key, exc_info=exc
        )
    except BaseException:
        pass


def _run_capture(work: Any) -> str | None:
    if getattr(_recursion_guard, "active", False):
        return None
    _recursion_guard.active = True
    try:
        return work()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        _log_internal_once(type(exc).__name__, exc)
        return None
    finally:
        _recursion_guard.active = False


def _is_ignored_logger(name: str) -> bool:
    if name == "admin_errors" or name.startswith("admin_errors."):
        return True
    for entry in conf.IGNORE_LOGGERS:
        if entry.endswith("."):
            if name.startswith(entry):
                return True
        elif name == entry:
            return True
    return False


def _resolve_ignore_exception_types() -> list[type[BaseException]]:
    resolved = []
    for path in conf.IGNORE_EXCEPTIONS:
        cls = _ignore_exception_types_cache.get(path, _MISSING)
        if cls is _MISSING:
            try:
                cls = import_string(path)
            except BaseException:
                cls = None
            _ignore_exception_types_cache[path] = cls
        if cls is not None:
            resolved.append(cls)
    return resolved


def _is_ignored_exception_type(exc_type: type[BaseException] | None) -> bool:
    if exc_type is None:
        return False
    return any(issubclass(exc_type, ignored) for ignored in _resolve_ignore_exception_types())


def _capture_in_debug_disabled() -> bool:
    return bool(django_settings.DEBUG) and not conf.CAPTURE_IN_DEBUG


def _mark_captured_once(exc_value: BaseException) -> bool:
    """`True` if this is the first time this exact exception object is seen."""
    if getattr(exc_value, "__admin_errors_captured__", False):
        return False
    try:
        exc_value.__admin_errors_captured__ = True
    except BaseException:
        pass  # e.g. a __slots__ exception class: dedup is lost, never an error
    return True


def _exc_qualname(exc_type: type[BaseException] | None) -> str:
    try:
        return exc_type.__qualname__
    except BaseException:
        return getattr(exc_type, "__name__", "Exception")


def _title(message: str) -> str:
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), "")
    return first_line[:500]


def _resolve_request(
    *, record: logging.LogRecord | None, exc_value: BaseException | None
) -> HttpRequest | None:
    if record is not None:
        request = getattr(record, "request", None)
        if request is not None:
            return request
    if exc_value is not None:
        request = getattr(exc_value, "__admin_errors_request__", None)
        if request is not None:
            return request
    return context.get_current_request()


def _apply_before_send(
    payload: dict[str, Any],
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    tb: TracebackType | None,
    record: logging.LogRecord | None,
) -> dict[str, Any] | None:
    hook_setting = conf.BEFORE_SEND
    if not hook_setting:
        return payload
    try:
        hook = import_string(hook_setting) if isinstance(hook_setting, str) else hook_setting
        hint = {
            "exc_info": (exc_type, exc_value, tb) if exc_value is not None else None,
            "record": record,
        }
        result = hook(payload, hint)
    except BaseException as exc:
        _log_internal_once(f"before_send.{type(exc).__name__}", exc)
        return None
    return result


def _size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, default=str).encode("utf-8"))


def _all_frame_lists(payload: dict[str, Any]) -> list[list[dict[str, Any]]]:
    lists = [payload.get("frames", [])]
    exception = payload.get("exception")
    if exception:
        lists.extend(node.get("frames", []) for node in exception.get("chain", []))
    return lists


def _enforce_size(payload: dict[str, Any]) -> dict[str, Any]:
    # `payload["frames"]` is the same list object as `payload["exception"]["chain"][0]["frames"]`
    # (spec section 6.4 documents both keys), so it serialises twice; the effective frame/vars
    # budget under `MAX_PAYLOAD_BYTES` is roughly half of the configured byte count. Intentional
    # per the documented schema (see DECISIONS.md p03-review_fix1/context), not a bug to silently
    # fix by dropping a documented key.
    limit = conf.MAX_PAYLOAD_BYTES
    if _size(payload) <= limit:
        return payload

    for frames in _all_frame_lists(payload):
        for frame in frames:
            frame.pop("vars", None)
    if _size(payload) <= limit:
        return payload

    for frames in _all_frame_lists(payload):
        for frame in frames:
            frame.pop("pre_context", None)
            frame.pop("post_context", None)
            frame.pop("context_line", None)
    if _size(payload) <= limit:
        return payload

    if "message" in payload:
        payload["message"] = payload["message"][:200]
    exception = payload.get("exception")
    if exception:
        if "value" in exception:
            exception["value"] = exception["value"][:200]
        for node in exception.get("chain", []):
            if "value" in node:
                node["value"] = node["value"][:200]
    if _size(payload) <= limit:
        return payload

    minimal: dict[str, Any] = {
        "v": payload.get("v", 1),
        "timestamp": payload.get("timestamp"),
        "level": payload.get("level"),
        "logger": payload.get("logger"),
        "source": payload.get("source"),
        "message": (payload.get("message") or "")[:200],
        "truncated": True,
    }
    if exception:
        minimal["exception"] = {
            "type": exception.get("type"),
            "module": exception.get("module"),
            "value": (exception.get("value") or "")[:200],
        }
    return minimal


def _store(
    fingerprint_value: str,
    meta: dict[str, str],
    timestamp: datetime.datetime,
    payload: dict[str, Any],
) -> str:
    date = timestamp.astimezone(datetime.timezone.utc).date()
    aggregate = storage.Aggregate(
        meta=meta,
        count=1,
        first_ts=timestamp,
        last_ts=timestamp,
        samples=[(timestamp, payload)],
        dates={date: 1},
    )
    storage.store_batch({fingerprint_value: aggregate}, using=conf.DATABASE)
    return fingerprint_value


def _build_and_store(
    *,
    source: str,
    fp: str,
    exception_type: str,
    title: str,
    culprit: str,
    level: str,
    logger_name: str,
    formatted_message: str,
    request: HttpRequest | None,
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    tb: TracebackType | None,
    record: logging.LogRecord | None,
    extra: dict[str, Any] | None,
) -> str | None:
    meta = {
        "exception_type": exception_type,
        "title": title,
        "culprit": culprit,
        "level": level,
        "logger": logger_name,
    }
    timestamp = timezone.now()
    payload = context.build_payload(
        source=source,
        level=level,
        logger=logger_name,
        message=formatted_message,
        timestamp=timestamp,
        request=request,
        exc_type=exc_type,
        exc_value=exc_value,
        tb=tb,
        record=record,
    )
    if extra:
        merged_extra = dict(payload.get("extra") or {})
        merged_extra.update(
            {
                key: context.safe_repr(value, limit=conf.MAX_VAR_REPR_LENGTH)
                for key, value in extra.items()
            }
        )
        payload["extra"] = merged_extra

    payload = _apply_before_send(payload, exc_type, exc_value, tb, record)
    if payload is None:
        return None
    payload = _enforce_size(payload)
    return _store(fp, meta, timestamp, payload)


def _capture_record(record: logging.LogRecord) -> str | None:
    logger_name = record.name
    if not conf.ENABLED or _is_ignored_logger(logger_name) or _capture_in_debug_disabled():
        return None

    status_code = getattr(record, "status_code", None)
    if status_code is not None and status_code < conf.IGNORE_HTTP_STATUS_BELOW:
        return None

    exc_type = exc_value = tb = None
    if record.exc_info:
        exc_type, exc_value, tb = record.exc_info

    if exc_value is not None:
        if _is_ignored_exception_type(exc_type):
            return None
        if not _mark_captured_once(exc_value):
            return None

    try:
        formatted_message = record.getMessage()
    except BaseException:
        formatted_message = str(getattr(record, "msg", ""))

    request = _resolve_request(record=record, exc_value=exc_value)

    fingerprint_override = getattr(record, "fingerprint", None)
    if fingerprint_override:
        fp = fingerprint.for_override(fingerprint_override)
        culprit = context.select_culprit(tb) if tb is not None else ""
    elif exc_value is not None:
        culprit = context.select_culprit(tb)
        fp = fingerprint.for_exception(exc_type, culprit, str(exc_value))
    else:
        culprit = ""
        fp = fingerprint.for_message(logger_name, record.levelname, record.msg)

    exception_type = _exc_qualname(exc_type) if exc_value is not None else logger_name
    source = "exception" if exc_value is not None else "message"

    return _build_and_store(
        source=source,
        fp=fp,
        exception_type=exception_type,
        title=_title(formatted_message),
        culprit=culprit,
        level=_normalize_level(record.levelname),
        logger_name=logger_name,
        formatted_message=formatted_message,
        request=request,
        exc_type=exc_type,
        exc_value=exc_value,
        tb=tb,
        record=record,
        extra=None,
    )


def _capture_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    tb: TracebackType | None,
    *,
    request: HttpRequest | None,
    extra: dict[str, Any] | None,
    fingerprint_override: object,
    level: str,
) -> str | None:
    if not conf.ENABLED or _capture_in_debug_disabled():
        return None
    if _is_ignored_exception_type(exc_type):
        return None
    if not _mark_captured_once(exc_value):
        return None

    resolved_request = (
        request if request is not None else _resolve_request(record=None, exc_value=exc_value)
    )
    culprit = context.select_culprit(tb)
    fp = (
        fingerprint.for_override(fingerprint_override)
        if fingerprint_override
        else fingerprint.for_exception(exc_type, culprit, str(exc_value))
    )
    try:
        formatted_message = str(exc_value)
    except BaseException:
        formatted_message = context.safe_repr(exc_value, limit=conf.MAX_VAR_REPR_LENGTH)

    return _build_and_store(
        source="exception",
        fp=fp,
        exception_type=_exc_qualname(exc_type),
        title=_title(formatted_message),
        culprit=culprit,
        level=_normalize_level(level),
        logger_name=_API_LOGGER,
        formatted_message=formatted_message,
        request=resolved_request,
        exc_type=exc_type,
        exc_value=exc_value,
        tb=tb,
        record=None,
        extra=extra,
    )


def _capture_message(
    message: str,
    *,
    level: str,
    request: HttpRequest | None,
    extra: dict[str, Any] | None,
    fingerprint_override: object,
) -> str | None:
    if not conf.ENABLED or _capture_in_debug_disabled():
        return None

    resolved_request = request if request is not None else context.get_current_request()
    text = str(message)
    fp = (
        fingerprint.for_override(fingerprint_override)
        if fingerprint_override
        else fingerprint.for_message(_API_LOGGER, level.upper(), text)
    )

    return _build_and_store(
        source="message",
        fp=fp,
        exception_type=_API_LOGGER,
        title=_title(text),
        culprit="",
        level=_normalize_level(level),
        logger_name=_API_LOGGER,
        formatted_message=text,
        request=resolved_request,
        exc_type=None,
        exc_value=None,
        tb=None,
        record=None,
        extra=extra,
    )


def capture_record(record: logging.LogRecord) -> str | None:
    return _run_capture(lambda: _capture_record(record))


def capture_exception_info(
    exc_type: type[BaseException],
    exc_value: BaseException,
    tb: TracebackType | None,
    *,
    request: HttpRequest | None = None,
    extra: dict[str, Any] | None = None,
    fingerprint: object = None,
    level: str = "error",
) -> str | None:
    return _run_capture(
        lambda: _capture_exception(
            exc_type,
            exc_value,
            tb,
            request=request,
            extra=extra,
            fingerprint_override=fingerprint,
            level=level,
        )
    )


def capture_message_info(
    message: str,
    *,
    level: str = "error",
    request: HttpRequest | None = None,
    extra: dict[str, Any] | None = None,
    fingerprint: object = None,
) -> str | None:
    return _run_capture(
        lambda: _capture_message(
            message,
            level=level,
            request=request,
            extra=extra,
            fingerprint_override=fingerprint,
        )
    )
