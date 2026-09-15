"""`store_batch`: the only place that writes issues, events and daily counts (spec section 9.2).

One short `transaction.atomic(using=alias)` per aggregate, not per batch, so one bad row does not
lose the rest of the batch. Every create that can race another process is inside its own nested
`atomic()` savepoint, required on PostgreSQL so a racing `IntegrityError` does not poison the outer
transaction. No `select_for_update` anywhere: SQLite ignores it and the select-then-update pattern
does not need it.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F
from django.db.models.functions import Greatest

from admin_errors.conf import settings as conf
from admin_errors.models import Event, Issue, IssueDailyCount


@dataclasses.dataclass
class Aggregate:
    """One fingerprint's worth of occurrences collected since the last flush.

    `dates` maps each UTC date in `[first_ts, last_ts]` to how many of `count` occurrences fall on
    it — `first_ts`/`last_ts` alone name the two dates a midnight-spanning batch touches but not how
    to split `count` between the resulting `IssueDailyCount` rows.
    """

    meta: dict[str, str]
    count: int
    first_ts: datetime.datetime
    last_ts: datetime.datetime
    samples: list[tuple[datetime.datetime, dict[str, Any]]]
    dates: dict[datetime.date, int]


def store_batch(batch: dict[str, Aggregate], *, using: str | None = None) -> None:
    alias = using or conf.DATABASE
    for fingerprint, aggregate in batch.items():
        _store_one(fingerprint, aggregate, alias)


def _store_one(fingerprint: str, aggregate: Aggregate, alias: str) -> None:
    with transaction.atomic(using=alias):
        row = _select_issue(fingerprint, alias)
        if row is None:
            row = _create_issue(fingerprint, aggregate, alias)
        issue_id = row["id"]

        if row["status"] == Issue.Status.IGNORED:
            _update_counters(issue_id, aggregate, alias, status=Issue.Status.IGNORED)
            _update_daily_counts(issue_id, aggregate, alias)
            return

        reopen = row["status"] == Issue.Status.RESOLVED
        _update_counters(issue_id, aggregate, alias, status=row["status"], reopen=reopen)

        if aggregate.samples:
            _store_events(issue_id, aggregate, alias)

        _update_daily_counts(issue_id, aggregate, alias)


def _select_issue(fingerprint: str, alias: str) -> dict[str, Any] | None:
    return (
        Issue.objects.using(alias)
        .filter(fingerprint=fingerprint)
        .values("id", "status", "resolved_at", "notified_at")
        .first()
    )


def _create_issue(fingerprint: str, aggregate: Aggregate, alias: str) -> dict[str, Any]:
    meta = aggregate.meta
    last_event = aggregate.samples[-1][1] if aggregate.samples else None
    try:
        with transaction.atomic(using=alias):
            issue = Issue.objects.using(alias).create(
                fingerprint=fingerprint,
                exception_type=meta.get("exception_type", ""),
                title=meta.get("title", ""),
                culprit=meta.get("culprit", ""),
                level=meta.get("level", Issue.Level.ERROR),
                first_seen=aggregate.first_ts,
                last_seen=aggregate.first_ts,
                count=0,
                last_event=last_event,
            )
    except IntegrityError:
        # Another process created the same fingerprint concurrently; re-read what it wrote. A
        # `None` here means the `IntegrityError` was not the fingerprint race (e.g. a NOT NULL or
        # FK violation from a malformed meta dict): re-raise so the real cause isn't lost, instead
        # of stripped by `python -O` or masked as a `TypeError` in the caller.
        row = _select_issue(fingerprint, alias)
        if row is None:
            raise
        return row
    return {
        "id": issue.id,
        "status": issue.status,
        "resolved_at": issue.resolved_at,
        "notified_at": issue.notified_at,
    }


def _update_counters(
    issue_id: int,
    aggregate: Aggregate,
    alias: str,
    *,
    status: str,
    reopen: bool = False,
) -> None:
    fields: dict[str, Any] = {
        "count": F("count") + aggregate.count,
        "last_seen": Greatest(F("last_seen"), aggregate.last_ts),
    }
    if status == Issue.Status.IGNORED:
        Issue.objects.using(alias).filter(pk=issue_id).update(**fields)
        return
    if reopen:
        fields["status"] = Issue.Status.OPEN
    if aggregate.samples:
        fields["last_event"] = max(aggregate.samples, key=lambda sample: sample[0])[1]
    Issue.objects.using(alias).filter(pk=issue_id).update(**fields)


def _store_events(issue_id: int, aggregate: Aggregate, alias: str) -> None:
    Event.objects.using(alias).bulk_create(
        Event(issue_id=issue_id, timestamp=ts, payload=payload) for ts, payload in aggregate.samples
    )
    overflow_ids = list(
        Event.objects.using(alias)
        .filter(issue_id=issue_id)
        .order_by("-timestamp", "-id")
        .values_list("id", flat=True)[conf.EVENTS_PER_ISSUE :]
    )
    if overflow_ids:
        Event.objects.using(alias).filter(id__in=overflow_ids).delete()


def _update_daily_counts(issue_id: int, aggregate: Aggregate, alias: str) -> None:
    for date, count in aggregate.dates.items():
        updated = (
            IssueDailyCount.objects.using(alias)
            .filter(issue_id=issue_id, date=date)
            .update(count=F("count") + count)
        )
        if updated:
            continue
        try:
            with transaction.atomic(using=alias):
                IssueDailyCount.objects.using(alias).create(
                    issue_id=issue_id, date=date, count=count
                )
        except IntegrityError:
            IssueDailyCount.objects.using(alias).filter(issue_id=issue_id, date=date).update(
                count=F("count") + count
            )
