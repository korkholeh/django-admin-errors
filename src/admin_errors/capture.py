"""The single chokepoint every entry point funnels through (spec section 7.2).

Nothing here may ever raise or recurse: the whole pipeline runs inside its own
`try/except BaseException` (re-raising `KeyboardInterrupt`/`SystemExit`), guarded by a thread-local
recursion flag. Steps 4-5 (admission, sampling) bound a storm to one issue and a handful of events
before the expensive payload (step 6) is built; step 7 dispatches to the writer thread under
`TRANSPORT="thread"`, or writes inline otherwise.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import json
import logging
import threading
import time
from types import TracebackType
from typing import Any

from django.conf import settings as django_settings
from django.http import HttpRequest
from django.utils import timezone
from django.utils.module_loading import import_string

from admin_errors import context, fingerprint, storage, writer
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

# Admission and sampling state (spec section 7.2 steps 4-5). Bounded LRUs of 10 000 entries: an
# already-seen fingerprint is always admitted; a miss is only admitted while `_new_issue_bucket`
# has a token. Neither structure is locked (see DECISIONS.md p04-plan/capture): the architecture's
# risk-3 mitigation forbids a shared lock on this path, and an interleaved read-modify-write costs
# at most one extra/missing sample, within the "counts are approximate by design" contract.
_RATE_LIMIT_LRU_SIZE = 10_000


class _TokenBucket:
    """Capacity and period are re-read from `conf` on every call, so `override_settings` takes
    effect immediately. Capacity `None` means unlimited; capacity `<= 0` means no token is ever
    granted (see DECISIONS.md p04-plan/capture)."""

    def __init__(self, capacity_setting: str, period: float) -> None:
        self._capacity_setting = capacity_setting
        self._period = period
        self._tokens: float | None = None
        self._last_refill = 0.0

    def take(self) -> bool:
        capacity = getattr(conf, self._capacity_setting)
        if capacity is None:
            return True
        if capacity <= 0:
            return False
        now = time.monotonic()
        if self._tokens is None:
            self._tokens = float(capacity)
        else:
            elapsed = max(now - self._last_refill, 0.0)
            refilled = self._tokens + elapsed * (capacity / self._period)
            self._tokens = min(float(capacity), refilled)
        self._last_refill = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False


_seen_fingerprints: collections.OrderedDict[str, None] = collections.OrderedDict()
_new_issue_bucket = _TokenBucket("NEW_ISSUES_PER_MINUTE", 60)
_sample_buckets: collections.OrderedDict[str, _TokenBucket] = collections.OrderedDict()


def reset_rate_limits() -> None:
    """Clear admission/sampling state so budgets never leak between tests."""
    global _new_issue_bucket
    _seen_fingerprints.clear()
    _sample_buckets.clear()
    _new_issue_bucket = _TokenBucket("NEW_ISSUES_PER_MINUTE", 60)


def mark_thread_internal() -> None:
    """Permanently mark the calling thread as internal to admin_errors (the writer thread).

    Unlike the per-call recursion guard, this flag is never cleared: anything the writer thread
    itself logs at ERROR (e.g. an internal-logging warning) must not re-enter capture.
    """
    _recursion_guard.active = True


def _admit_fingerprint(fp: str) -> bool:
    if fp in _seen_fingerprints:
        _seen_fingerprints.move_to_end(fp)
        return True
    if not _new_issue_bucket.take():
        writer.stats.dropped_new_issue += 1
        return False
    _seen_fingerprints[fp] = None
    if len(_seen_fingerprints) > _RATE_LIMIT_LRU_SIZE:
        _seen_fingerprints.popitem(last=False)
    return True


def _should_sample(fp: str) -> bool:
    bucket = _sample_buckets.get(fp)
    if bucket is None:
        bucket = _TokenBucket("EVENT_SAMPLE_PER_HOUR", 3600)
        _sample_buckets[fp] = bucket
        if len(_sample_buckets) > _RATE_LIMIT_LRU_SIZE:
            _sample_buckets.popitem(last=False)
    else:
        _sample_buckets.move_to_end(fp)
    return bucket.take()


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


def _dispatch(
    fingerprint_value: str,
    meta: dict[str, str],
    timestamp: datetime.datetime,
    payload: dict[str, Any] | None,
) -> str:
    if conf.TRANSPORT == "thread":
        writer.get_writer().enqueue(
            CapturedItem(
                fingerprint=fingerprint_value, meta=meta, timestamp=timestamp, payload=payload
            )
        )
        return fingerprint_value

    date = timestamp.astimezone(datetime.timezone.utc).date()
    aggregate = storage.Aggregate(
        meta=meta,
        count=1,
        first_ts=timestamp,
        last_ts=timestamp,
        samples=[(timestamp, payload)] if payload is not None else [],
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
    celery: dict[str, Any] | None = None,
) -> str | None:
    meta = {
        "exception_type": exception_type,
        "title": title,
        "culprit": culprit,
        "level": level,
        "logger": logger_name,
    }
    timestamp = timezone.now()

    if not _admit_fingerprint(fp):
        return None

    if not _should_sample(fp):
        return _dispatch(fp, meta, timestamp, None)

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
        celery=celery,
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
    return _dispatch(fp, meta, timestamp, payload)


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
    celery: dict[str, Any] | None = None,
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
        celery=celery,
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
    celery: dict[str, Any] | None = None,
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
            celery=celery,
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
