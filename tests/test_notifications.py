from __future__ import annotations

import datetime as dt
import socket
import threading
from typing import ClassVar
from unittest import mock

import pytest
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from admin_errors import notifications, signals, storage, textformat, writer
from admin_errors.models import Issue


def _payload(**overrides):
    payload = {
        "v": 1,
        "message": "boom",
        "exception": {
            "type": "ValueError",
            "module": "builtins",
            "value": "boom",
            "chain": [
                {
                    "type": "ValueError",
                    "module": "builtins",
                    "value": "boom",
                    "cause": False,
                    "frames": [
                        {
                            "filename": "/app/views.py",
                            "lineno": 10,
                            "function": "outer",
                            "module": "app.views",
                            "in_app": True,
                            "context_line": "call_inner()",
                            "vars": {"token": "'secret'"},
                        },
                        {
                            "filename": "/app/views.py",
                            "lineno": 42,
                            "function": "inner",
                            "module": "app.views",
                            "in_app": True,
                            "context_line": "raise ValueError(message)",
                            "vars": {"message": "'boom'"},
                        },
                    ],
                }
            ],
        },
    }
    payload.update(overrides)
    return payload


class TestFormatTracebackText:
    def test_frames_innermost_last(self):
        text = textformat.format_traceback_text(_payload())
        outer_pos = text.index('File "/app/views.py", line 10, in outer')
        inner_pos = text.index('File "/app/views.py", line 42, in inner')
        assert outer_pos < inner_pos
        assert 'File "/app/views.py", line 42, in inner' in text
        assert "builtins.ValueError: boom" in text

    def test_include_locals_true_shows_a_local(self):
        text = textformat.format_traceback_text(_payload(), include_locals=True)
        assert "token = 'secret'" in text

    def test_include_locals_false_never_shows_a_local(self):
        text = textformat.format_traceback_text(_payload(), include_locals=False)
        assert "token" not in text
        assert "'secret'" not in text

    def test_max_frames_keeps_the_innermost_n(self):
        text = textformat.format_traceback_text(_payload(), max_frames=1)
        assert "line 42, in inner" in text
        assert "line 10, in outer" not in text

    def test_chained_entries_get_a_separator(self):
        payload = _payload()
        payload["exception"]["chain"] = [
            {
                "type": "ValueError",
                "module": "builtins",
                "value": "outer",
                "cause": True,  # chain[0] (outer) was raised with explicit `from` chain[1] (root).
                "frames": [],
            },
            {
                "type": "KeyError",
                "module": "builtins",
                "value": "'k'",
                "cause": False,
                "frames": [],
            },
        ]
        text = textformat.format_traceback_text(payload)
        assert "The above exception was the direct cause of the following exception:" in text
        # root cause (KeyError, chain[1]) renders first, the outer ValueError second.
        assert text.index("builtins.KeyError") < text.index("builtins.ValueError")

    def test_implicit_context_link_uses_the_context_separator(self):
        payload = _payload()
        payload["exception"]["chain"] = [
            {"type": "A", "module": "m", "value": "a", "cause": False, "frames": []},
            {"type": "B", "module": "m", "value": "b", "cause": False, "frames": []},
        ]
        text = textformat.format_traceback_text(payload)
        assert "During handling of the above exception, another exception occurred:" in text

    def test_message_only_payload_renders_without_raising(self):
        text = textformat.format_traceback_text({"v": 1, "message": "just a log line"})
        assert text == "just a log line"

    def test_none_payload_renders_without_raising(self):
        assert textformat.format_traceback_text(None) == ""

    def test_empty_payload_renders_without_raising(self):
        assert textformat.format_traceback_text({}) == ""


pytestmark = pytest.mark.django_db


def _agg(**overrides) -> storage.Aggregate:
    now = overrides.pop("_now", None) or timezone.now()
    payload = overrides.pop("payload", None) or _payload()
    defaults = {
        "meta": {
            "exception_type": "ValueError",
            "title": "boom",
            "culprit": "shop.views.checkout",
            "level": "error",
        },
        "count": 1,
        "first_ts": now,
        "last_ts": now,
        "samples": [(now, payload)],
        "dates": {now.astimezone(dt.timezone.utc).date(): 1},
    }
    defaults.update(overrides)
    return storage.Aggregate(**defaults)


class _RecordingNotifier:
    calls: ClassVar[list[tuple[str, str]]] = []

    def notify(self, *, issue, event, reason):
        self.calls.append((issue.fingerprint, reason))
        return True


class _RaisingNotifier:
    def notify(self, *, issue, event, reason):
        raise RuntimeError("boom-notify")


@pytest.fixture(autouse=True)
def _admins():
    with override_settings(ADMINS=[("Admin", "admin@example.com")]):
        yield


class TestEmailNotifierEndToEnd:
    """Drives the real `storage.store_batch` -> signal -> `EmailNotifier` chain (spec section
    14.2)."""

    def test_new_issue_sends_one_email_to_admins(self):
        with TestCase.captureOnCommitCallbacks(execute=True):
            storage.store_batch({"fp-notify-created": _agg()})

        assert len(mail.outbox) == 1
        message = mail.outbox[0]
        assert message.to == ["admin@example.com"]
        assert message.subject.startswith(f"[{socket.gethostname()}] ")
        assert "New issue:" in message.subject
        assert "ValueError" in message.subject
        assert "shop.views.checkout" in message.subject

        issue = Issue.objects.get(fingerprint="fp-notify-created")
        assert issue.title in message.body
        assert issue.culprit in message.body
        assert str(issue.count) in message.body
        assert issue.first_seen.isoformat() in message.body
        assert issue.last_seen.isoformat() in message.body
        assert 'File "/app/views.py", line 42, in inner' in message.body
        assert reverse("admin:admin_errors_issue_change", args=[issue.pk]) in message.body
        assert issue.notified_at is not None

    def test_regression_sends_one_email(self):
        with override_settings(ADMIN_ERRORS={"NOTIFY_THROTTLE_SECONDS": 0}):
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-regress": _agg()})
            mail.outbox.clear()

            issue = Issue.objects.get(fingerprint="fp-notify-regress")
            issue.status = Issue.Status.RESOLVED
            issue.save(update_fields=["status"])

            later = timezone.now() + dt.timedelta(hours=1)
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-regress": _agg(_now=later)})

        assert len(mail.outbox) == 1
        assert "Regression:" in mail.outbox[0].subject

    def test_ignored_issue_sends_no_email(self):
        with TestCase.captureOnCommitCallbacks(execute=True):
            storage.store_batch({"fp-notify-ignored": _agg()})
        mail.outbox.clear()

        issue = Issue.objects.get(fingerprint="fp-notify-ignored")
        issue.status = Issue.Status.IGNORED
        issue.save(update_fields=["status"])

        with TestCase.captureOnCommitCallbacks(execute=True):
            storage.store_batch({"fp-notify-ignored": _agg()})

        assert mail.outbox == []

    def test_throttle_suppresses_second_notification_inside_window(self):
        base = timezone.now()
        with mock.patch("admin_errors.notifications.timezone.now", return_value=base):
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-throttle": _agg(_now=base)})
        assert len(mail.outbox) == 1
        mail.outbox.clear()

        issue = Issue.objects.get(fingerprint="fp-notify-throttle")
        issue.status = Issue.Status.RESOLVED
        issue.save(update_fields=["status"])

        soon = base + dt.timedelta(seconds=10)
        with mock.patch("admin_errors.notifications.timezone.now", return_value=soon):
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-throttle": _agg(_now=soon)})

        assert mail.outbox == []

    def test_notification_after_throttle_window(self):
        base = timezone.now()
        with mock.patch("admin_errors.notifications.timezone.now", return_value=base):
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-after-throttle": _agg(_now=base)})
        assert len(mail.outbox) == 1
        mail.outbox.clear()

        issue = Issue.objects.get(fingerprint="fp-notify-after-throttle")
        issue.status = Issue.Status.RESOLVED
        issue.save(update_fields=["status"])

        after_window = base + dt.timedelta(seconds=3700)  # > default NOTIFY_THROTTLE_SECONDS=3600
        with mock.patch("admin_errors.notifications.timezone.now", return_value=after_window):
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-after-throttle": _agg(_now=after_window)})

        assert len(mail.outbox) == 1
        assert "Regression:" in mail.outbox[0].subject

    def test_notify_base_url_is_prefixed_onto_the_admin_link(self):
        with override_settings(ADMIN_ERRORS={"NOTIFY_BASE_URL": "https://errors.example.com"}):
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-base-url": _agg()})

        issue = Issue.objects.get(fingerprint="fp-notify-base-url")
        path = reverse("admin:admin_errors_issue_change", args=[issue.pk])
        assert f"https://errors.example.com{path}" in mail.outbox[0].body

    def test_notify_recipients_overrides_admins(self):
        with override_settings(ADMIN_ERRORS={"NOTIFY_RECIPIENTS": ["ops@example.com"]}):
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-recipients": _agg()})

        assert mail.outbox[0].to == ["ops@example.com"]

    def test_notify_on_regressed_only_suppresses_created_email(self):
        with override_settings(ADMIN_ERRORS={"NOTIFY_ON": ["regressed"]}):
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-on-regressed-only": _agg()})

        assert mail.outbox == []

    def test_notify_backend_none_disables_and_disconnects_receivers(self):
        with override_settings(ADMIN_ERRORS={"NOTIFY_BACKEND": None}):
            assert not signals.issue_created.has_listeners()
            assert not signals.issue_regressed.has_listeners()
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-backend-none": _agg()})

        assert mail.outbox == []

    def test_custom_notify_backend_is_used(self):
        _RecordingNotifier.calls = []
        with override_settings(
            ADMIN_ERRORS={"NOTIFY_BACKEND": "tests.test_notifications._RecordingNotifier"}
        ):
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-custom": _agg()})

        assert _RecordingNotifier.calls == [("fp-notify-custom", "created")]
        assert mail.outbox == []

    def test_custom_notify_backend_is_used_without_admins_or_recipients(self):
        """A non-email `NOTIFY_BACKEND` has no `is_enabled` hook, so it must stay connected even
        when nothing is configured for `EmailNotifier`'s own recipients (review r1 major #2:
        `refresh_connections()` must not gate the generic extension point on `_recipients()`)."""
        _RecordingNotifier.calls = []
        with override_settings(
            ADMINS=[],
            ADMIN_ERRORS={
                "NOTIFY_BACKEND": "tests.test_notifications._RecordingNotifier",
                "NOTIFY_RECIPIENTS": None,
            },
        ):
            assert signals.issue_created.has_listeners()
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-custom-no-admins": _agg()})

        assert _RecordingNotifier.calls == [("fp-notify-custom-no-admins", "created")]
        assert mail.outbox == []

    def test_backend_raising_does_not_break_store_batch(self):
        writer.stats.receiver_errors = 0
        with override_settings(
            ADMIN_ERRORS={"NOTIFY_BACKEND": "tests.test_notifications._RaisingNotifier"}
        ):
            with TestCase.captureOnCommitCallbacks(execute=True):
                storage.store_batch({"fp-notify-raising": _agg()})

        assert Issue.objects.filter(fingerprint="fp-notify-raising").exists()
        assert writer.stats.receiver_errors == 1


@pytest.mark.django_db(transaction=True)
def test_two_threads_notifying_send_one_email():
    """Two threads race `EmailNotifier.notify()` for the same issue; only the one whose `UPDATE`
    touches `notified_at` sends (spec section 10, matches `test_storage.py`'s race pattern)."""
    issue = Issue.objects.create(
        fingerprint="fp-notify-race",
        exception_type="ValueError",
        title="boom",
        culprit="shop.views.checkout",
        level=Issue.Level.ERROR,
        first_seen=timezone.now(),
        last_seen=timezone.now(),
        count=1,
    )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    results: list[bool] = []

    def worker() -> None:
        from django.db import connection

        try:
            barrier.wait(timeout=5.0)
            sent = notifications.EmailNotifier().notify(issue=issue, event=None, reason="created")
            results.append(sent)
        except BaseException as exc:  # surfaced via `errors`, not swallowed
            errors.append(exc)
        finally:
            connection.close()

    with override_settings(ADMINS=[("Admin", "admin@example.com")]):
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

    assert not errors
    assert results.count(True) == 1
    assert len(mail.outbox) == 1
    updated = Issue.objects.get(pk=issue.pk)
    assert updated.notified_at is not None
