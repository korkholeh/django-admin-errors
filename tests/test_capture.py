"""`capture.py` / `api.py` / `handlers.py` exercised end to end through `tests/views.py`.

`Client(raise_request_exception=False)` is what makes an unhandled view exception behave like a
real deployment: Django logs it through the `django.request` logger (caught by `AdminErrorsHandler`
on the root logger) and returns a 500 response instead of re-raising into the test.
"""

from __future__ import annotations

import logging

import pytest
from asgiref.sync import async_to_sync
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.test import AsyncClient, Client, override_settings

from admin_errors import api, capture, writer
from admin_errors.apps import AdminErrorsConfig
from admin_errors.handlers import AdminErrorsHandler
from admin_errors.models import Event, Issue

pytestmark = pytest.mark.django_db

WITH_REQUEST_MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "admin_errors.middleware.RequestContextMiddleware",
]


def _make_record(
    *,
    name: str = "tests.direct",
    level: int = logging.ERROR,
    msg: str = "direct message",
    exc_info=None,
    status_code: int | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(name, level, __file__, 1, msg, None, exc_info)
    if status_code is not None:
        record.status_code = status_code
    return record


def _exc_info(exc: BaseException):
    return (type(exc), exc, exc.__traceback__)


# --- T10: happy paths -------------------------------------------------------------------------


def test_unhandled_view_exception_creates_one_issue_with_request_context():
    client = Client(raise_request_exception=False)
    response = client.get("/boom/")

    assert response.status_code == 500
    assert Issue.objects.count() == 1
    issue = Issue.objects.get()
    assert issue.count == 1
    event = Event.objects.get(issue=issue)
    assert event.payload["source"] == "exception"
    assert event.payload["logger"] == "django.request"
    assert event.payload["request"]["path"] == "/boom/"


def test_logger_exception_without_middleware_has_no_request_block():
    client = Client(raise_request_exception=False)
    response = client.get("/log-only/")

    assert response.status_code == 200
    issue = Issue.objects.get()
    event = Event.objects.get(issue=issue)
    assert event.payload["source"] == "exception"
    assert "request" not in event.payload


def test_logger_exception_with_middleware_has_request_block():
    client = Client(raise_request_exception=False)
    with override_settings(MIDDLEWARE=WITH_REQUEST_MIDDLEWARE):
        response = client.get("/log-only/")

    assert response.status_code == 200
    issue = Issue.objects.get()
    event = Event.objects.get(issue=issue)
    assert event.payload["request"]["path"] == "/log-only/"


def test_capture_message_stores_source_message_and_extra():
    fp = api.capture_message("hello world", extra={"order_id": 42})

    assert fp is not None
    issue = Issue.objects.get(fingerprint=fp)
    event = Event.objects.get(issue=issue)
    assert event.payload["source"] == "message"
    assert event.payload["extra"]["order_id"] == "42"


def test_capture_message_sanitizes_nul_in_extra_key():
    """A NUL in an `extra` key reaches the stored payload after the merge in `_build_and_store`,
    past the point `build_payload()` used to sanitize alone — the late, once-only
    `context.sanitize_payload` call must still catch it (DECISIONS.md p06-review_fix1/context)."""
    fp = api.capture_message("hello", extra={"bad\x00key": "value"})

    assert fp is not None
    issue = Issue.objects.get(fingerprint=fp)
    event = Event.objects.get(issue=issue)
    assert "bad�key" in event.payload["extra"]
    assert not any("\x00" in key for key in event.payload["extra"])


def test_capture_exception_with_no_active_exception_returns_none():
    assert api.capture_exception() is None
    assert Issue.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_async_view_exception_is_captured_with_request_context():
    client = AsyncClient(raise_request_exception=False)

    async def _do_request():
        with override_settings(MIDDLEWARE=WITH_REQUEST_MIDDLEWARE):
            return await client.get("/aboom/")

    response = async_to_sync(_do_request)()

    assert response.status_code == 500
    issue = Issue.objects.get()
    event = Event.objects.get(issue=issue)
    assert event.payload["request"]["path"] == "/aboom/"


# --- T11: drop and safety paths ----------------------------------------------------------------


def test_ignored_exception_types_are_dropped():
    # A request to /notfound/ or /denied/ never even reaches the pipeline: Django logs 404/403 at
    # WARNING (below the default CAPTURE_LEVEL="ERROR") and IGNORE_HTTP_STATUS_BELOW=500 would drop
    # it regardless. Exercise `IGNORE_EXCEPTIONS` directly instead so the guard itself is tested.
    assert api.capture_exception(Http404("not found")) is None
    assert api.capture_exception(PermissionDenied("denied")) is None
    assert Issue.objects.count() == 0


def test_ignored_exception_types_cover_subclasses():
    class CustomNotFound(Http404):
        pass

    assert api.capture_exception(CustomNotFound("custom")) is None
    assert Issue.objects.count() == 0


def test_ignore_exceptions_positive_control_and_unresolvable_dotted_path():
    with override_settings(
        ADMIN_ERRORS={
            "IGNORE_EXCEPTIONS": ["not.a.real.module.NopeError"],
            "TRANSPORT": "sync",
        }
    ):
        fp = api.capture_exception(Http404("now captured"))

    assert fp is not None
    assert Issue.objects.count() == 1


def test_ignore_loggers_exact_and_prefix():
    try:
        raise ValueError("boom")
    except ValueError as exc:
        record = _make_record(name="tests.direct", exc_info=_exc_info(exc))
        with override_settings(ADMIN_ERRORS={"IGNORE_LOGGERS": ["tests.direct"]}):
            assert capture.capture_record(record) is None
        assert Issue.objects.count() == 0

        record = _make_record(name="tests.direct.sub", exc_info=_exc_info(exc))
        with override_settings(ADMIN_ERRORS={"IGNORE_LOGGERS": ["tests."]}):
            assert capture.capture_record(record) is None
        assert Issue.objects.count() == 0


def test_records_below_the_status_threshold_are_dropped():
    record = _make_record(status_code=404)
    assert capture.capture_record(record) is None
    assert Issue.objects.count() == 0


def test_log_and_reraise_produces_one_occurrence():
    client = Client(raise_request_exception=False)
    response = client.get("/log-and-raise/")

    assert response.status_code == 500
    assert Issue.objects.count() == 1
    assert Issue.objects.get().count == 1


def test_enabled_false_is_a_noop():
    try:
        raise ValueError("boom")
    except ValueError as exc:
        record = _make_record(exc_info=_exc_info(exc))
        with override_settings(ADMIN_ERRORS={"ENABLED": False}):
            assert capture.capture_record(record) is None
    assert Issue.objects.count() == 0


def test_capture_in_debug_false_is_a_noop():
    try:
        raise ValueError("boom")
    except ValueError as exc:
        record = _make_record(exc_info=_exc_info(exc))
        with override_settings(DEBUG=True, ADMIN_ERRORS={"CAPTURE_IN_DEBUG": False}):
            assert capture.capture_record(record) is None
    assert Issue.objects.count() == 0

    try:
        raise ValueError("boom again")
    except ValueError as exc:
        record = _make_record(exc_info=_exc_info(exc))
        with override_settings(
            DEBUG=True, ADMIN_ERRORS={"CAPTURE_IN_DEBUG": True, "TRANSPORT": "sync"}
        ):
            assert capture.capture_record(record) is not None
    assert Issue.objects.count() == 1


def test_before_send_can_mutate_the_payload():
    def add_marker(payload, hint):
        payload["extra"] = {**(payload.get("extra") or {}), "marked": "yes"}
        return payload

    with override_settings(ADMIN_ERRORS={"BEFORE_SEND": add_marker, "TRANSPORT": "sync"}):
        fp = api.capture_message("mutate me")

    event = Event.objects.get(issue__fingerprint=fp)
    assert event.payload["extra"]["marked"] == "yes"


def test_before_send_returning_none_drops_the_item():
    with override_settings(ADMIN_ERRORS={"BEFORE_SEND": lambda payload, hint: None}):
        fp = api.capture_message("drop me")

    assert fp is None
    assert Issue.objects.count() == 0


def test_storage_failure_does_not_propagate_or_recurse(monkeypatch):
    calls = []

    def raise_on_store(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("db is down")

    monkeypatch.setattr("admin_errors.storage.store_batch", raise_on_store)

    client = Client(raise_request_exception=False)
    # INTERNAL_LOGGING=True makes the failure log a WARNING to "admin_errors.internal" through the
    # very same root handler; if that re-entered the pipeline, `store_batch` would be called twice.
    with override_settings(ADMIN_ERRORS={"INTERNAL_LOGGING": True, "TRANSPORT": "sync"}):
        response = client.get("/boom/")

    assert response.status_code == 500
    assert Issue.objects.count() == 0
    assert len(calls) == 1


def _deeply_nested_raise():
    def _inner():
        def _innermost():
            raise ValueError("boom")

        _innermost()

    _inner()


def test_max_frames_bounds_the_stored_frame_count():
    # Same code raises both times, so both captures share one fingerprint (same type, culprit,
    # message) and land as two `Event` rows on the same `Issue`; disambiguate by insertion order
    # rather than by fingerprint.
    try:
        _deeply_nested_raise()
    except ValueError as exc:
        fp = api.capture_exception(exc)

    unbounded_event = Event.objects.get(issue__fingerprint=fp)
    assert len(unbounded_event.payload["frames"]) >= 3

    with override_settings(ADMIN_ERRORS={"MAX_FRAMES": 1, "TRANSPORT": "sync"}):
        try:
            _deeply_nested_raise()
        except ValueError as exc:
            api.capture_exception(exc)

    bounded_event = Event.objects.exclude(pk=unbounded_event.pk).get(issue__fingerprint=fp)
    frames = bounded_event.payload["frames"]
    assert len(frames) == 1
    # `build_frames` keeps the innermost frames, not the outermost ones.
    assert frames[0]["function"] == "_innermost"


def test_max_var_repr_length_truncates_locals():
    with override_settings(ADMIN_ERRORS={"MAX_VAR_REPR_LENGTH": 5, "TRANSPORT": "sync"}):
        try:
            long_local = "x" * 1000  # noqa: F841 - captured via the traceback locals
            raise ValueError("boom")
        except ValueError as exc:
            fp = api.capture_exception(exc)

    event = Event.objects.get(issue__fingerprint=fp)
    reprs = event.payload["frames"][-1].get("vars", {})
    assert all(len(value) <= 6 for value in reprs.values())  # limit + the "…" marker


def test_payload_degrades_to_the_minimal_shape_under_a_tiny_byte_cap():
    with override_settings(ADMIN_ERRORS={"MAX_PAYLOAD_BYTES": 50, "TRANSPORT": "sync"}):
        try:
            raise ValueError("a very long message " * 20)
        except ValueError as exc:
            fp = api.capture_exception(exc)

    event = Event.objects.get(issue__fingerprint=fp)
    assert event.payload["truncated"] is True
    assert "frames" not in event.payload
    assert "request" not in event.payload


def test_unrepresentable_local_var_becomes_the_fallback_string_end_to_end():
    client = Client(raise_request_exception=False)
    response = client.get("/unrepresentable/")

    assert response.status_code == 500
    event = Event.objects.get()
    obj_repr = event.payload["frames"][-1]["vars"]["obj"]
    assert obj_repr.startswith("<unrepresentable")


def test_capture_level_below_warning_lowers_the_root_logger():
    root = logging.getLogger()
    original_level = root.level
    installed = [handler for handler in root.handlers if isinstance(handler, AdminErrorsHandler)]
    for handler in installed:
        root.removeHandler(handler)
    try:
        with override_settings(ADMIN_ERRORS={"CAPTURE_LEVEL": "INFO"}):
            AdminErrorsConfig._install_logging_handler()
        assert root.getEffectiveLevel() <= logging.INFO
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, AdminErrorsHandler):
                root.removeHandler(handler)
        for handler in installed:
            root.addHandler(handler)
        root.setLevel(original_level)


def test_debug_record_is_stored_with_a_level_in_issue_choices():
    """`CAPTURE_LEVEL="DEBUG"` makes a DEBUG record reachable (see the test above); its level name
    is outside `Issue.Level`'s four choices and must be mapped rather than stored verbatim."""
    with override_settings(ADMIN_ERRORS={"CAPTURE_LEVEL": "DEBUG", "TRANSPORT": "sync"}):
        fp = capture.capture_record(_make_record(level=logging.DEBUG, msg="debug detail"))

    issue = Issue.objects.get(fingerprint=fp)
    assert issue.level in Issue.Level.values
    assert issue.level == Issue.Level.INFO


def test_capture_message_with_unknown_level_clamps_to_error():
    fp = api.capture_message("weird level", level="somethinglong")

    issue = Issue.objects.get(fingerprint=fp)
    assert issue.level in Issue.Level.values
    assert issue.level == Issue.Level.ERROR


# --- Admission and sampling (spec section 7.2 steps 4-5) --------------------------------------


def test_event_sampling_caps_stored_events_not_the_count():
    with override_settings(ADMIN_ERRORS={"EVENT_SAMPLE_PER_HOUR": 2, "TRANSPORT": "sync"}):
        fp = None
        for _ in range(10):
            fp = api.capture_message("sampled repeatedly", fingerprint="sampled-fp")

    issue = Issue.objects.get(fingerprint=fp)
    assert issue.count == 10
    assert Event.objects.filter(issue=issue).count() == 2


def test_new_issue_admission_drops_and_counts():
    with override_settings(ADMIN_ERRORS={"NEW_ISSUES_PER_MINUTE": 3, "TRANSPORT": "sync"}):
        for i in range(10):
            api.capture_message(f"unique admission error {i}", fingerprint=f"admission-fp-{i}")

    assert Issue.objects.count() == 3
    assert writer.stats.dropped_new_issue == 7


def test_already_seen_fingerprint_is_admitted_with_an_empty_new_issue_bucket():
    """Positive pair for the refusal case: a truly new fingerprint is dropped under the same
    exhausted bucket that still lets an already-seen one through."""
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync"}):
        fp = api.capture_message("seen before", fingerprint="seen-fp")

    with override_settings(ADMIN_ERRORS={"NEW_ISSUES_PER_MINUTE": 0, "TRANSPORT": "sync"}):
        again = api.capture_message("seen before", fingerprint="seen-fp")
        dropped = api.capture_message("never seen before", fingerprint="brand-new-fp")

    assert again == fp
    assert dropped is None
    assert Issue.objects.count() == 1
    assert Issue.objects.get(fingerprint=fp).count == 2
    assert writer.stats.dropped_new_issue == 1


def test_event_sample_per_hour_zero_still_creates_the_issue_count_only():
    with override_settings(ADMIN_ERRORS={"EVENT_SAMPLE_PER_HOUR": 0, "TRANSPORT": "sync"}):
        fp = api.capture_message("count only", fingerprint="count-only-fp")

    issue = Issue.objects.get(fingerprint=fp)
    assert issue.count == 1
    assert not Event.objects.filter(issue=issue).exists()


def test_sample_bucket_refills_after_the_configured_period(monkeypatch):
    fake_time = [1_000.0]
    monkeypatch.setattr(capture.time, "monotonic", lambda: fake_time[0])

    with override_settings(ADMIN_ERRORS={"EVENT_SAMPLE_PER_HOUR": 1, "TRANSPORT": "sync"}):
        fp = api.capture_message("refill test", fingerprint="refill-fp")
        api.capture_message("refill test", fingerprint="refill-fp")
        assert Event.objects.filter(issue__fingerprint=fp).count() == 1

        fake_time[0] += 3_600.0
        api.capture_message("refill test", fingerprint="refill-fp")
        assert Event.objects.filter(issue__fingerprint=fp).count() == 2

    assert Issue.objects.get(fingerprint=fp).count == 3


@pytest.mark.django_db(transaction=True)
def test_thread_transport_end_to_end_via_client_and_flush():
    client = Client(raise_request_exception=False)
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "thread"}):
        response = client.get("/boom/")
        assert response.status_code == 500
        api.flush()

    assert Issue.objects.count() == 1
