"""`storage.store_batch`: spec section 9.2. `TestCase` already wraps each test in a transaction, so
`transaction.atomic()` inside `store_batch` only ever emits `SAVEPOINT`/`RELEASE`, never `BEGIN`.
"""

import datetime as dt

import pytest
from django.db import IntegrityError
from django.test import override_settings
from django.utils import timezone

from admin_errors import storage
from admin_errors.models import Event, Issue, IssueDailyCount

pytestmark = pytest.mark.django_db


def _agg(**overrides) -> storage.Aggregate:
    now = overrides.pop("_now", None) or timezone.now()
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
        "samples": [(now, {"v": 1, "message": "boom"})],
        "dates": {now.astimezone(dt.timezone.utc).date(): 1},
    }
    defaults.update(overrides)
    return storage.Aggregate(**defaults)


def test_create_path_sets_issue_fields_from_meta():
    agg = _agg()
    storage.store_batch({"fp-create": agg})

    issue = Issue.objects.get(fingerprint="fp-create")
    assert issue.exception_type == "ValueError"
    assert issue.title == "boom"
    assert issue.culprit == "shop.views.checkout"
    assert issue.level == "error"
    assert issue.status == Issue.Status.OPEN
    assert issue.count == 1
    assert issue.first_seen == agg.first_ts
    assert issue.last_seen == agg.first_ts
    assert issue.last_event == agg.samples[0][1]
    assert Event.objects.filter(issue=issue).count() == 1
    assert IssueDailyCount.objects.get(issue=issue).count == 1


def test_update_path_increments_counters_and_last_seen():
    agg1 = _agg()
    storage.store_batch({"fp-update": agg1})
    later = agg1.first_ts + dt.timedelta(hours=1)
    agg2 = _agg(
        _now=later,
        count=1,
        samples=[(later, {"v": 1, "message": "boom again"})],
        dates={later.astimezone(dt.timezone.utc).date(): 1},
    )
    storage.store_batch({"fp-update": agg2})

    issue = Issue.objects.get(fingerprint="fp-update")
    assert issue.count == 2
    assert issue.last_seen == later
    assert issue.last_event == agg2.samples[0][1]


def test_first_seen_is_unchanged_on_update():
    agg1 = _agg()
    storage.store_batch({"fp-first-seen": agg1})
    later = agg1.first_ts + dt.timedelta(days=1)
    agg2 = _agg(_now=later, samples=[(later, {"v": 1, "message": "later"})])
    storage.store_batch({"fp-first-seen": agg2})

    issue = Issue.objects.get(fingerprint="fp-first-seen")
    assert issue.first_seen == agg1.first_ts


def test_event_ring_buffer_keeps_the_newest_events_per_issue():
    base = timezone.now()
    samples = [(base + dt.timedelta(seconds=i), {"v": 1, "message": f"e{i}"}) for i in range(5)]
    agg = _agg(
        count=5,
        first_ts=samples[0][0],
        last_ts=samples[-1][0],
        samples=samples,
        dates={base.astimezone(dt.timezone.utc).date(): 5},
    )
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync", "EVENTS_PER_ISSUE": 3}):
        storage.store_batch({"fp-ring": agg})

    issue = Issue.objects.get(fingerprint="fp-ring")
    assert Event.objects.filter(issue=issue).count() == 3
    kept = set(Event.objects.filter(issue=issue).values_list("payload__message", flat=True))
    assert kept == {"e2", "e3", "e4"}
    assert issue.last_event == {"v": 1, "message": "e4"}


def test_batch_spanning_utc_midnight_creates_two_daily_rows():
    day1 = dt.datetime(2026, 1, 1, 23, 59, tzinfo=dt.timezone.utc)
    day2 = dt.datetime(2026, 1, 2, 0, 1, tzinfo=dt.timezone.utc)
    agg = _agg(
        count=2,
        first_ts=day1,
        last_ts=day2,
        samples=[(day1, {"v": 1, "message": "a"}), (day2, {"v": 1, "message": "b"})],
        dates={day1.date(): 1, day2.date(): 1},
    )
    storage.store_batch({"fp-midnight": agg})

    issue = Issue.objects.get(fingerprint="fp-midnight")
    rows = {row.date: row.count for row in IssueDailyCount.objects.filter(issue=issue)}
    assert rows == {day1.date(): 1, day2.date(): 1}


def test_resolved_issue_reopens_and_keeps_resolved_at():
    agg = _agg()
    storage.store_batch({"fp-resolved": agg})
    issue = Issue.objects.get(fingerprint="fp-resolved")
    resolved_at = timezone.now()
    issue.status = Issue.Status.RESOLVED
    issue.resolved_at = resolved_at
    issue.save(update_fields=["status", "resolved_at"])

    later = agg.first_ts + dt.timedelta(hours=1)
    agg2 = _agg(_now=later, samples=[(later, {"v": 1, "message": "regressed"})])
    storage.store_batch({"fp-resolved": agg2})

    issue.refresh_from_db()
    assert issue.status == Issue.Status.OPEN
    assert issue.resolved_at == resolved_at
    assert issue.count == 2


def test_ignored_issue_gets_counters_only():
    agg = _agg()
    storage.store_batch({"fp-ignored": agg})
    issue = Issue.objects.get(fingerprint="fp-ignored")
    issue.status = Issue.Status.IGNORED
    issue.save(update_fields=["status"])

    later = agg.first_ts + dt.timedelta(hours=1)
    agg2 = _agg(_now=later, samples=[(later, {"v": 1, "message": "again"})])
    storage.store_batch({"fp-ignored": agg2})

    issue.refresh_from_db()
    assert issue.status == Issue.Status.IGNORED
    assert issue.count == 2
    assert issue.last_seen == later
    assert (
        issue.last_event == agg.samples[0][1]
    )  # unchanged: ignored issues skip step 4's last_event
    # 1 event from the initial (pre-ignore) create batch; the ignored batch adds no new events.
    assert Event.objects.filter(issue=issue).count() == 1
    total_daily = sum(IssueDailyCount.objects.filter(issue=issue).values_list("count", flat=True))
    assert total_daily == 2


def test_unresolved_integrity_error_during_create_propagates(monkeypatch):
    """An `IntegrityError` that is not the fingerprint race (e.g. a NOT NULL/FK violation) must
    propagate with its original traceback, not be swallowed by a stripped `assert` under `-O`."""

    class _RaisingManager:
        def create(self, **kwargs):
            raise IntegrityError("not a fingerprint race")

    monkeypatch.setattr(storage.Issue.objects, "using", lambda alias: _RaisingManager())
    monkeypatch.setattr(storage, "_select_issue", lambda fingerprint, alias: None)

    with pytest.raises(IntegrityError):
        storage._create_issue("fp-unresolved-integrity", _agg(), "default")


def test_query_budget_new_issue_with_one_sample(django_assert_max_num_queries):
    agg = _agg()
    # Observed on SQLite (verified with CaptureQueriesContext, not just the earlier comment's
    # guess): 1 savepoint + 1 select + 1 savepoint + 1 insert (issue) + 1 release + 1 update
    # (counters) + 1 insert (event) + 1 select (trim) + 1 update (daily, miss) + 1 savepoint +
    # 1 insert (daily) + 1 release + 1 release = 13. Bound left at observed + 2 so a per-row
    # insert loop still trips it.
    with django_assert_max_num_queries(15):
        storage.store_batch({"fp-budget-new": agg})


def test_query_budget_count_only(django_assert_max_num_queries):
    agg = _agg()
    storage.store_batch({"fp-budget-count": agg})
    later = agg.first_ts + dt.timedelta(hours=1)
    count_only = _agg(_now=later, samples=[], dates={later.astimezone(dt.timezone.utc).date(): 1})
    # Observed: 1 savepoint + 1 select + 1 update (counters) + 1 update (daily, hits) +
    # 1 release = 5.
    with django_assert_max_num_queries(7):
        storage.store_batch({"fp-budget-count": count_only})


def test_query_budget_existing_issue_with_three_samples(django_assert_max_num_queries):
    agg = _agg()
    storage.store_batch({"fp-budget-3": agg})
    base = agg.first_ts + dt.timedelta(hours=1)
    samples = [(base + dt.timedelta(seconds=i), {"v": 1, "message": f"s{i}"}) for i in range(3)]
    agg2 = _agg(
        _now=base,
        count=3,
        first_ts=samples[0][0],
        last_ts=samples[-1][0],
        samples=samples,
        dates={base.astimezone(dt.timezone.utc).date(): 3},
    )
    # Observed: 1 savepoint + 1 select + 1 update (counters) + 1 insert (events, bulk) +
    # 1 select (trim, no delete) + 1 update (daily) + 1 release = 7. Bound left at observed + 1
    # (not +2: a per-row insert loop over 3 samples turns the single bulk insert into 3, i.e. 9
    # queries, which must still trip this bound) so it leaves room for one extra PostgreSQL
    # savepoint statement while still catching a per-row insert loop or a per-date N+1.
    with django_assert_max_num_queries(8):
        storage.store_batch({"fp-budget-3": agg2})
