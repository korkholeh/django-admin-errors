"""`retention.run_cleanup` (spec section 9.3) and the writer's opportunistic cleanup hook.

Every rule is tested under a frozen clock (`mock.patch("django.utils.timezone.now")`), so "older
than N days" is a fact about the test data, not about when the suite happens to run.
"""

from __future__ import annotations

import datetime
import uuid
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import override_settings

from admin_errors import retention, writer
from admin_errors.models import Event, Issue, IssueDailyCount

pytestmark = pytest.mark.django_db

NOW = datetime.datetime(2026, 6, 15, 12, 0, tzinfo=datetime.timezone.utc)


def _frozen_now():
    return mock.patch("django.utils.timezone.now", return_value=NOW)


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


def _event(issue: Issue, *, timestamp: datetime.datetime) -> Event:
    return Event.objects.create(issue=issue, timestamp=timestamp, payload={"v": 1})


def _daily(issue: Issue, *, date: datetime.date, count: int = 1) -> IssueDailyCount:
    return IssueDailyCount.objects.create(issue=issue, date=date, count=count)


def _days_ago(days: float) -> datetime.datetime:
    return NOW - datetime.timedelta(days=days)


# --- Rule 1: events --------------------------------------------------------------------------


def test_rule1_events_older_than_retention_deleted():
    issue = _issue()
    old = _event(issue, timestamp=_days_ago(31))  # EVENT_RETENTION_DAYS default is 30
    new = _event(issue, timestamp=_days_ago(1))
    with _frozen_now():
        report = retention.run_cleanup()
    assert report.events == 1
    assert not Event.objects.filter(pk=old.pk).exists()
    assert Event.objects.filter(pk=new.pk).exists()


def test_2500_stale_events_delete_in_chunks_within_query_bound(django_assert_max_num_queries):
    issue = _issue()
    stale = _days_ago(31)
    Event.objects.bulk_create(
        Event(issue=issue, timestamp=stale, payload={"v": 1}) for _ in range(2500)
    )
    with _frozen_now():
        # Rule 1 alone: 4 selects (three 1000-id chunks plus the final empty check) and 3 fast
        # deletes (Event has no signal receivers and nothing cascades through it, so each chunk
        # deletes in a single query, no SELECT-then-DELETE) = 7, independent of the 2500 row count.
        # The other five rules touch no matching row here (rule 4 is skipped outright:
        # IGNORED_ISSUE_TTL_DAYS is None by default) and cost one query each = +5. Total 12.
        with django_assert_max_num_queries(12):
            report = retention.run_cleanup()
    assert report.events == 2500
    assert Event.objects.count() == 0


# --- Rule 2: daily counts ---------------------------------------------------------------------


def test_rule2_daily_counts_older_than_retention_deleted():
    issue = _issue()
    old = _daily(issue, date=_days_ago(91).date())  # DAILY_COUNT_RETENTION_DAYS default is 90
    new = _daily(issue, date=_days_ago(1).date())
    with _frozen_now():
        report = retention.run_cleanup()
    assert report.daily_counts == 1
    assert not IssueDailyCount.objects.filter(pk=old.pk).exists()
    assert IssueDailyCount.objects.filter(pk=new.pk).exists()


# --- Rule 3: resolved issue TTL ----------------------------------------------------------------


def test_rule3_resolved_issue_ttl_deleted_and_recently_seen_kept():
    # RESOLVED_ISSUE_TTL_DAYS default is 14.
    stale = _issue(status=Issue.Status.RESOLVED, resolved_at=_days_ago(15), last_seen=_days_ago(15))
    recently_seen = _issue(
        status=Issue.Status.RESOLVED, resolved_at=_days_ago(15), last_seen=_days_ago(1)
    )
    null_resolved_at_but_stale = _issue(
        status=Issue.Status.RESOLVED, resolved_at=None, last_seen=_days_ago(15)
    )
    with _frozen_now():
        report = retention.run_cleanup()
    assert report.resolved_issues == 2
    assert not Issue.objects.filter(pk=stale.pk).exists()
    assert Issue.objects.filter(pk=recently_seen.pk).exists()
    assert not Issue.objects.filter(pk=null_resolved_at_but_stale.pk).exists()


def test_rule3_cascade_deletes_events_and_daily_counts_without_counting_them():
    stale = _issue(status=Issue.Status.RESOLVED, resolved_at=_days_ago(15), last_seen=_days_ago(15))
    _event(stale, timestamp=_days_ago(15))
    _daily(stale, date=_days_ago(15).date())
    with _frozen_now():
        report = retention.run_cleanup()
    assert report.resolved_issues == 1
    assert report.events == 0
    assert report.daily_counts == 0
    assert not Issue.objects.filter(pk=stale.pk).exists()
    assert Event.objects.count() == 0
    assert IssueDailyCount.objects.count() == 0


# --- Rule 4: ignored issue TTL -----------------------------------------------------------------


def test_rule4_ignored_issue_deleted_when_ttl_set():
    stale = _issue(status=Issue.Status.IGNORED, last_seen=_days_ago(31))
    with _frozen_now(), override_settings(ADMIN_ERRORS={"IGNORED_ISSUE_TTL_DAYS": 30}):
        report = retention.run_cleanup()
    assert report.ignored_issues == 1
    assert not Issue.objects.filter(pk=stale.pk).exists()


def test_rule4_ignored_issue_kept_when_ttl_is_none():
    stale = _issue(status=Issue.Status.IGNORED, last_seen=_days_ago(365))
    with _frozen_now():
        report = retention.run_cleanup()  # IGNORED_ISSUE_TTL_DAYS defaults to None
    assert report.ignored_issues == 0
    assert Issue.objects.filter(pk=stale.pk).exists()


# --- Rule 5: open issue TTL --------------------------------------------------------------------


def test_rule5_open_issue_ttl_deleted_and_recent_kept():
    # OPEN_ISSUE_TTL_DAYS default is 90.
    stale = _issue(status=Issue.Status.OPEN, last_seen=_days_ago(91))
    recent = _issue(status=Issue.Status.OPEN, last_seen=_days_ago(1))
    with _frozen_now():
        report = retention.run_cleanup()
    assert report.open_issues == 1
    assert not Issue.objects.filter(pk=stale.pk).exists()
    assert Issue.objects.filter(pk=recent.pk).exists()


# --- Rule 6: eviction --------------------------------------------------------------------------


def test_eviction_order_ignored_then_resolved_then_open_down_to_hysteresis():
    ignored_old = _issue(
        status=Issue.Status.IGNORED, last_seen=NOW - datetime.timedelta(minutes=50)
    )
    ignored_new = _issue(
        status=Issue.Status.IGNORED, last_seen=NOW - datetime.timedelta(minutes=40)
    )
    resolved_old = _issue(
        status=Issue.Status.RESOLVED,
        resolved_at=NOW,
        last_seen=NOW - datetime.timedelta(minutes=30),
    )
    resolved_new = _issue(
        status=Issue.Status.RESOLVED,
        resolved_at=NOW,
        last_seen=NOW - datetime.timedelta(minutes=20),
    )
    open_issues = [
        _issue(status=Issue.Status.OPEN, last_seen=NOW - datetime.timedelta(minutes=i))
        for i in range(8)
    ]
    # 2 ignored + 2 resolved + 8 open = 12 rows; MAX_ISSUES=10 -> target = int(10*0.9) = 9 ->
    # evict 3: both ignored, then the single oldest resolved.
    with _frozen_now(), override_settings(ADMIN_ERRORS={"MAX_ISSUES": 10}):
        report = retention.run_cleanup()
    assert report.evicted_issues == 3
    assert not Issue.objects.filter(pk=ignored_old.pk).exists()
    assert not Issue.objects.filter(pk=ignored_new.pk).exists()
    assert not Issue.objects.filter(pk=resolved_old.pk).exists()
    assert Issue.objects.filter(pk=resolved_new.pk).exists()
    assert Issue.objects.filter(pk__in=[o.pk for o in open_issues]).count() == 8


def test_no_eviction_at_exactly_max_issues():
    for i in range(10):
        _issue(status=Issue.Status.OPEN, last_seen=NOW - datetime.timedelta(minutes=i))
    with _frozen_now(), override_settings(ADMIN_ERRORS={"MAX_ISSUES": 10}):
        report = retention.run_cleanup()
    assert report.evicted_issues == 0
    assert Issue.objects.count() == 10


def test_eviction_chunks_past_the_sql_variable_limit():
    # Regression for the OperationalError: too many SQL variables bug (review round 1, MAJOR):
    # _evict_status must delete in CHUNK_SIZE-sized passes rather than binding one giant id list.
    Issue.objects.bulk_create(
        Issue(
            fingerprint=uuid.uuid4().hex,
            exception_type="ValueError",
            title="boom",
            culprit="x",
            level=Issue.Level.ERROR,
            status=Issue.Status.OPEN,
            first_seen=NOW,
            last_seen=NOW - datetime.timedelta(minutes=i),
            count=1,
        )
        for i in range(2500)
    )
    with _frozen_now(), override_settings(ADMIN_ERRORS={"MAX_ISSUES": 100}):
        report = retention.run_cleanup()
    target = int(100 * retention.HYSTERESIS)
    assert report.evicted_issues == 2500 - target
    assert Issue.objects.count() == target


def test_max_issues_none_evicts_nothing():
    for i in range(20):
        _issue(status=Issue.Status.OPEN, last_seen=NOW - datetime.timedelta(minutes=i))
    with _frozen_now(), override_settings(ADMIN_ERRORS={"MAX_ISSUES": None}):
        report = retention.run_cleanup()
    assert report.evicted_issues == 0
    assert Issue.objects.count() == 20


# --- Rule 7: vacuum ----------------------------------------------------------------------------


def test_incremental_vacuum_runs_when_auto_vacuum_is_2(sqlite_alias):
    alias = sqlite_alias(auto_vacuum=2)
    with _frozen_now():
        report = retention.run_cleanup(using=alias)
    assert report.vacuum == "incremental"
    assert Issue.objects.using(alias).count() == 0  # still queryable


def test_no_incremental_vacuum_when_auto_vacuum_is_0(sqlite_alias):
    alias = sqlite_alias(auto_vacuum=0)
    with _frozen_now():
        report = retention.run_cleanup(using=alias)
    assert report.vacuum is None


def test_no_vacuum_when_sqlite_vacuum_is_off(sqlite_alias):
    alias = sqlite_alias(auto_vacuum=2)
    with _frozen_now(), override_settings(ADMIN_ERRORS={"SQLITE_VACUUM": "off"}):
        report = retention.run_cleanup(using=alias)
    assert report.vacuum is None


@pytest.mark.parametrize("auto_vacuum", [0, 2])
def test_full_vacuum_rewrites_and_database_still_usable(sqlite_alias, auto_vacuum):
    alias = sqlite_alias(auto_vacuum=auto_vacuum)
    with _frozen_now():
        report = retention.run_cleanup(using=alias, vacuum=True)
    assert report.vacuum == "full"
    assert Issue.objects.using(alias).count() == 0


def test_vacuum_is_a_noop_on_postgresql():
    from django.db import connection

    if connection.vendor != "postgresql":
        pytest.skip("only meaningful under DJANGO_DB=postgres")
    with _frozen_now():
        report = retention.run_cleanup(vacuum=True)
    assert report.vacuum is None


# --- dry_run -------------------------------------------------------------------------------------


def test_dry_run_reports_every_rule_and_deletes_nothing():
    event_issue = _issue()
    _event(event_issue, timestamp=_days_ago(31))
    _daily(event_issue, date=_days_ago(91).date())
    resolved_stale = _issue(
        status=Issue.Status.RESOLVED, resolved_at=_days_ago(15), last_seen=_days_ago(15)
    )
    ignored_stale = _issue(status=Issue.Status.IGNORED, last_seen=_days_ago(31))
    open_stale = _issue(status=Issue.Status.OPEN, last_seen=_days_ago(91))
    for _ in range(10):
        _issue(status=Issue.Status.OPEN, last_seen=NOW)

    before = (
        Issue.objects.count(),
        Event.objects.count(),
        IssueDailyCount.objects.count(),
    )
    with (
        _frozen_now(),
        override_settings(ADMIN_ERRORS={"IGNORED_ISSUE_TTL_DAYS": 30, "MAX_ISSUES": 5}),
    ):
        report = retention.run_cleanup(dry_run=True)

    assert report.dry_run is True
    assert report.events == 1
    assert report.daily_counts == 1
    assert report.resolved_issues == 1
    assert report.ignored_issues == 1
    assert report.open_issues == 1
    # 14 issues total, would_delete = 1+1+1 = 3, target = int(5*0.9) = 4 ->
    # max(0, 14 - 3 - 4) = 7.
    assert report.evicted_issues == 7
    assert report.vacuum is None
    after = (
        Issue.objects.count(),
        Event.objects.count(),
        IssueDailyCount.objects.count(),
    )
    assert after == before
    assert (
        Issue.objects.filter(pk__in=[resolved_stale.pk, ignored_stale.pk, open_stale.pk]).count()
        == 3
    )


def test_dry_run_eviction_estimate_matches_a_real_run():
    event_issue = _issue()
    _event(event_issue, timestamp=_days_ago(31))
    _daily(event_issue, date=_days_ago(91).date())
    _issue(status=Issue.Status.RESOLVED, resolved_at=_days_ago(15), last_seen=_days_ago(15))
    _issue(status=Issue.Status.IGNORED, last_seen=_days_ago(31))
    _issue(status=Issue.Status.OPEN, last_seen=_days_ago(91))
    for _ in range(10):
        _issue(status=Issue.Status.OPEN, last_seen=NOW)

    settings = override_settings(ADMIN_ERRORS={"IGNORED_ISSUE_TTL_DAYS": 30, "MAX_ISSUES": 5})
    with _frozen_now(), settings:
        dry_report = retention.run_cleanup(dry_run=True)
        real_report = retention.run_cleanup(dry_run=False)

    assert dry_report.evicted_issues == real_report.evicted_issues


# --- Opportunistic cleanup (writer._maybe_cleanup) ------------------------------------------------


def test_opportunistic_cleanup_runs_once_per_interval(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(retention, "run_cleanup", lambda **kw: calls.append(kw))
    cache.clear()
    w = writer.Writer()
    with override_settings(
        ADMIN_ERRORS={"CLEANUP": "opportunistic", "CLEANUP_INTERVAL_SECONDS": 3600}
    ):
        w._maybe_cleanup()
        w._maybe_cleanup()
        w._maybe_cleanup()
    assert len(calls) == 1
    assert writer.stats.cleanups == 1


def test_opportunistic_cleanup_runs_again_after_the_interval(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(retention, "run_cleanup", lambda **kw: calls.append(kw))
    monkeypatch.setattr(writer.cache, "add", lambda *a, **kw: True)
    fake_time = [1000.0]
    monkeypatch.setattr(writer.time, "monotonic", lambda: fake_time[0])
    w = writer.Writer()
    with override_settings(
        ADMIN_ERRORS={"CLEANUP": "opportunistic", "CLEANUP_INTERVAL_SECONDS": 60}
    ):
        w._maybe_cleanup()
        fake_time[0] += 61
        w._maybe_cleanup()
    assert len(calls) == 2


def test_opportunistic_cleanup_disabled_with_cleanup_off(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(retention, "run_cleanup", lambda **kw: calls.append(kw))
    w = writer.Writer()
    with override_settings(ADMIN_ERRORS={"CLEANUP": "off"}):
        w._maybe_cleanup()
    assert calls == []
    assert writer.stats.cleanups == 0


def test_cache_lock_stops_a_second_process(monkeypatch):
    """Two `Writer`s in two processes would each have their own `_last_cleanup=None`; simulated
    here by resetting the module-global between calls while the cache lock (the real cross-process
    resource) stays held."""
    calls: list[dict] = []
    monkeypatch.setattr(retention, "run_cleanup", lambda **kw: calls.append(kw))
    cache.clear()
    w1 = writer.Writer()
    w2 = writer.Writer()
    with override_settings(
        ADMIN_ERRORS={"CLEANUP": "opportunistic", "CLEANUP_INTERVAL_SECONDS": 3600}
    ):
        writer._last_cleanup = None
        w1._maybe_cleanup()
        writer._last_cleanup = None
        w2._maybe_cleanup()
    assert len(calls) == 1


def test_failing_cleanup_is_recorded_and_does_not_retry_before_interval(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(retention, "run_cleanup", _boom)
    cache.clear()
    w = writer.Writer()
    with override_settings(
        ADMIN_ERRORS={"CLEANUP": "opportunistic", "CLEANUP_INTERVAL_SECONDS": 3600}
    ):
        w._maybe_cleanup()
        w._maybe_cleanup()
    assert writer.stats.last_error == "RuntimeError"
    assert writer.stats.cleanups == 0
