"""The Celery `task_failure` integration and the `admin_errors.tasks.cleanup` beat task."""

from __future__ import annotations

import importlib
import sys

import pytest
from django.test import override_settings

pytest.importorskip("celery")

from celery import Celery

from admin_errors.integrations.celery import connect as connect_celery_integration
from admin_errors.models import Event, Issue

pytestmark = pytest.mark.django_db


@pytest.fixture
def celery_app():
    app = Celery("admin_errors_test")
    app.conf.broker_url = "memory://"
    app.conf.result_backend = "cache+memory://"
    app.conf.task_always_eager = True
    app.conf.task_eager_propagates = False
    return app


def test_eager_task_failure_creates_one_issue_with_celery_context(celery_app):
    # apps.ready() already connected the integration once at process start; connecting again must
    # be a no-op (dispatch_uid), not a duplicate capture.
    connect_celery_integration()

    @celery_app.task(name="admin_errors.tests.failing_task")
    def failing_task(x, y=None):
        raise ValueError("celery boom")

    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync"}):
        result = failing_task.apply_async(args=[1], kwargs={"y": 2})

    issue = Issue.objects.get()
    assert issue.count == 1
    assert issue.exception_type == "ValueError"

    event = Event.objects.get(issue=issue)
    assert event.payload["exception"]["type"] == "ValueError"
    celery_block = event.payload["celery"]
    assert celery_block["task"] == "admin_errors.tests.failing_task"
    assert celery_block["task_id"] == result.id
    assert celery_block["args"] == "[1]"
    assert celery_block["kwargs"] == "{'y': 2}"


def test_eager_task_failure_truncates_long_args_and_kwargs(celery_app):
    connect_celery_integration()

    @celery_app.task(name="admin_errors.tests.failing_task_long_args")
    def failing_task(payload):
        raise ValueError("celery boom, long payload")

    long_arg = "x" * 5000
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync"}):
        failing_task.apply_async(args=[long_arg])

    event = Event.objects.get()
    assert len(event.payload["celery"]["args"]) == 500


def test_capture_without_celery_kwarg_has_no_celery_key():
    from admin_errors import capture

    try:
        raise ValueError("plain, non-celery failure")
    except ValueError:
        with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync"}):
            fp = capture.capture_exception_info(*sys.exc_info())
    assert fp is not None
    event = Event.objects.get()
    assert "celery" not in event.payload


def test_tasks_cleanup_runs_retention_and_returns_report_dict():
    from admin_errors import tasks

    report = tasks.cleanup()
    assert isinstance(report, dict)
    assert set(report) >= {"events", "daily_counts", "resolved_issues", "vacuum"}


def test_tasks_module_still_imports_without_celery_and_defines_no_cleanup():
    # `importlib.reload` re-executes a module's code in its *existing* namespace: an attribute the
    # new pass doesn't reassign (like `cleanup`, guarded by `if shared_task is not None`) would
    # survive from the earlier, celery-present import. A fresh `import_module` after popping the
    # module from `sys.modules` avoids that stale-attribute trap.
    original_celery = sys.modules.get("celery")
    original_tasks = sys.modules.pop("admin_errors.tasks", None)
    sys.modules["celery"] = None
    try:
        tasks_module = importlib.import_module("admin_errors.tasks")
        assert not hasattr(tasks_module, "cleanup")
    finally:
        sys.modules.pop("admin_errors.tasks", None)
        if original_celery is not None:
            sys.modules["celery"] = original_celery
        else:
            sys.modules.pop("celery", None)
        if original_tasks is not None:
            sys.modules["admin_errors.tasks"] = original_tasks
        else:
            importlib.import_module("admin_errors.tasks")
