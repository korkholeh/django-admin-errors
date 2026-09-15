"""The three management commands: `errors_cleanup`, `errors_stats`, `errors_test`."""

from __future__ import annotations

import datetime
import io
import uuid

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings

from admin_errors.models import Event, Issue

pytestmark = pytest.mark.django_db

NOW = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


def _issue(**overrides) -> Issue:
    defaults = {
        "fingerprint": uuid.uuid4().hex,
        "exception_type": "ValueError",
        "title": "boom",
        "culprit": "x",
        "level": Issue.Level.ERROR,
        "status": Issue.Status.OPEN,
        "first_seen": NOW,
        "last_seen": NOW,
        "count": 1,
    }
    defaults.update(overrides)
    return Issue.objects.create(**defaults)


def _call(command: str, *args: str) -> str:
    out = io.StringIO()
    call_command(command, *args, stdout=out)
    return out.getvalue()


# --- errors_cleanup ---------------------------------------------------------------------------


def test_errors_cleanup_prints_counts_and_deletes():
    issue = _issue(status=Issue.Status.OPEN, last_seen=NOW)
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync", "OPEN_ISSUE_TTL_DAYS": 1}):
        output = _call("errors_cleanup")
    assert "open_issues: 1" in output
    assert "vacuum:" in output
    assert not Issue.objects.filter(pk=issue.pk).exists()


def test_errors_cleanup_dry_run_prints_counts_and_deletes_nothing():
    issue = _issue(status=Issue.Status.OPEN, last_seen=NOW)
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync", "OPEN_ISSUE_TTL_DAYS": 1}):
        output = _call("errors_cleanup", "--dry-run")
    assert "(dry run — nothing deleted)" in output
    assert "open_issues: 1" in output
    assert Issue.objects.filter(pk=issue.pk).exists()


def test_errors_cleanup_vacuum_warns_and_succeeds(sqlite_alias):
    alias = sqlite_alias()
    out = io.StringIO()
    err = io.StringIO()
    call_command("errors_cleanup", "--vacuum", "--database", alias, stdout=out, stderr=err)
    assert "vacuum" in err.getvalue().lower()
    assert "vacuum: full" in out.getvalue()


def test_errors_cleanup_vacuum_dry_run_does_not_warn_and_reports_skipped(sqlite_alias):
    alias = sqlite_alias()
    out = io.StringIO()
    err = io.StringIO()
    call_command(
        "errors_cleanup", "--vacuum", "--dry-run", "--database", alias, stdout=out, stderr=err
    )
    assert err.getvalue() == ""
    assert "vacuum: skipped (dry run)" in out.getvalue()


def test_errors_cleanup_unknown_database_raises_command_error():
    with pytest.raises(CommandError):
        call_command("errors_cleanup", "--database", "not-a-real-alias")


# --- errors_stats ------------------------------------------------------------------------------


def test_errors_stats_prints_counts_and_writer_stats():
    issue = _issue(status=Issue.Status.OPEN)
    Event.objects.create(issue=issue, timestamp=NOW, payload={"v": 1})
    output = _call("errors_stats")
    assert "issues: 1" in output
    assert "events: 1" in output
    assert "daily_counts: 0" in output
    assert "status[open]: 1" in output
    assert "approximate size:" in output
    assert "enqueued: 0" in output


def test_errors_stats_on_empty_database_prints_zeros():
    output = _call("errors_stats")
    assert "issues: 0" in output
    assert "events: 0" in output
    assert "daily_counts: 0" in output
    assert "oldest issue first_seen: n/a" in output


def test_errors_stats_unknown_database_raises_command_error():
    with pytest.raises(CommandError):
        call_command("errors_stats", "--database", "not-a-real-alias")


def test_errors_stats_approximate_size_on_postgresql():
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("only meaningful under DJANGO_DB=postgres")
    _issue(status=Issue.Status.OPEN)
    output = _call("errors_stats")
    assert "approximate size:" in output
    assert "bytes" in output


# --- errors_test ---------------------------------------------------------------------------------


def test_errors_test_creates_issue_and_prints_admin_url():
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync"}):
        output = _call("errors_test")
    assert Issue.objects.count() == 1
    assert "/admin/admin_errors/issue/" in output
    issue = Issue.objects.get()
    assert f"/admin/admin_errors/issue/{issue.pk}/change/" in output


def test_errors_test_with_enabled_false_raises_and_creates_nothing():
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync", "ENABLED": False}):
        with pytest.raises(CommandError, match="ENABLED"):
            call_command("errors_test")
    assert Issue.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_errors_test_under_thread_transport_still_finds_the_issue():
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "thread"}):
        output = _call("errors_test")
    assert Issue.objects.count() == 1
    assert "/admin/admin_errors/issue/" in output
