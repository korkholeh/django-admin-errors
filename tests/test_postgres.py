"""PostgreSQL-only coverage (Phase 6, spec section 3 "Compatibility and constraints"): everything
SQLite structurally cannot exercise — savepoint isolation across a swallowed `IntegrityError`,
JSONB round trips, and NUL / lone-surrogate payload sanitisation. Every case takes `postgres_only`
and is a no-op skip under the default SQLite run; only `DJANGO_DB=postgres` runs them for real
(see DECISIONS.md p06-plan/tests).
"""

import datetime as dt

import pytest
from django.db import transaction
from django.db.models.query import QuerySet
from django.test import override_settings
from django.utils import timezone

from admin_errors import api, storage
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


def _issue(fingerprint: str) -> Issue:
    now = timezone.now()
    return Issue.objects.create(
        fingerprint=fingerprint,
        exception_type="ValueError",
        title="boom",
        culprit="shop.views.checkout",
        level=Issue.Level.ERROR,
        first_seen=now,
        last_seen=now,
        count=0,
    )


def test_racing_issue_create_leaves_the_outer_transaction_usable(postgres_only):
    """The race's loser: `_create_issue` hits a real unique-constraint `IntegrityError` on the
    pre-existing fingerprint. Without the nested savepoint in `_create_issue`, PostgreSQL would
    abort the whole outer transaction and the follow-up query below would raise
    `InternalError: current transaction is aborted`."""
    existing = _issue("fp-race-create")

    with transaction.atomic():
        row, created = storage._create_issue("fp-race-create", _agg(), "default")

        assert created is False
        assert row["id"] == existing.id
        assert Issue.objects.filter(pk=existing.id).exists()


def test_racing_daily_count_create_leaves_the_outer_transaction_usable(postgres_only, monkeypatch):
    """Force the update-returns-0-then-`IntegrityError` branch of `_update_daily_counts` by making
    the first `QuerySet.update()` call in the block report "no row updated" even though one exists,
    so the savepointed `create()` collides for real. The second `update()` (the except branch) must
    run unpatched and actually add the count, proving the outer transaction survived."""
    issue = _issue("fp-race-daily")
    date = timezone.now().date()
    IssueDailyCount.objects.create(issue=issue, date=date, count=5)
    agg = _agg(dates={date: 3})

    original_update = QuerySet.update
    calls = {"n": 0}

    def fake_update(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0
        return original_update(self, **kwargs)

    monkeypatch.setattr(QuerySet, "update", fake_update)

    with transaction.atomic():
        storage._update_daily_counts(issue.id, agg, "default")
        assert Issue.objects.filter(pk=issue.id).exists()

    daily = IssueDailyCount.objects.get(issue=issue, date=date)
    assert daily.count == 8


def test_store_batch_survives_swallowed_integrity_error_in_one_outer_transaction(postgres_only):
    agg1 = _agg()
    later = agg1.first_ts + dt.timedelta(hours=1)
    agg2 = _agg(
        _now=later,
        samples=[(later, {"v": 1, "message": "boom again"})],
        dates={later.astimezone(dt.timezone.utc).date(): 1},
    )

    with transaction.atomic():
        storage.store_batch({"fp-outer": agg1})
        storage.store_batch({"fp-outer": agg2})

    assert Issue.objects.filter(fingerprint="fp-outer").count() == 1
    issue = Issue.objects.get(fingerprint="fp-outer")
    assert issue.count == 2


def test_jsonb_payload_round_trip_non_ascii_and_emoji(postgres_only):
    payload = {"message": "boom \U0001f4a5", "user": {"name": "Олег", "id": 1}, "tags": ["a", "b"]}
    agg = _agg(samples=[(timezone.now(), payload)])

    storage.store_batch({"fp-jsonb": agg})

    issue = Issue.objects.get(fingerprint="fp-jsonb")
    assert issue.last_event == payload
    event = Event.objects.get(issue=issue)
    assert event.payload == payload


def test_exception_message_with_nul_is_stored_on_postgresql(postgres_only):
    """Without `context.sanitize_text`, PostgreSQL rejects the NUL codepoint in both the `title`
    `text` column and the `last_event` `jsonb` column; SQLite would store it happily and never
    catch this (DECISIONS.md p06-plan/context)."""
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync"}):
        try:
            raise ValueError("boom\x00with a NUL byte")
        except ValueError as exc:
            fp = api.capture_exception(exc)

    assert fp is not None
    issue = Issue.objects.get(fingerprint=fp)
    assert "\x00" not in issue.title
    assert "�" in issue.title
    assert issue.last_event is not None
    assert "\x00" not in issue.last_event["exception"]["value"]


def test_exception_with_nul_in_query_param_name_is_stored_on_postgresql(rf, postgres_only):
    """A NUL in a query-string *key* (not value) reaches `build_request_block`'s `query` dict
    verbatim. Sanitizing only dict *values* (the original fix) leaves it untouched, so the
    `jsonb` write is rejected and the whole capture is silently swallowed by the pipeline's
    `try/except BaseException` — reproducible only on PostgreSQL (DECISIONS.md
    p06-review_fix1/context)."""
    request = rf.get("/x/?bad%00key=1")
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync"}):
        try:
            raise ValueError("boom")
        except ValueError as exc:
            fp = api.capture_exception(exc, request=request)

    assert fp is not None
    issue = Issue.objects.get(fingerprint=fp)
    assert issue.last_event is not None
    query = issue.last_event["request"]["query"]
    assert not any("\x00" in key for key in query)
    assert any("�" in key for key in query)
