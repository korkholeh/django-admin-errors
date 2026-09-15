"""Bounded storage without operator attention (spec section 9.3).

Six delete rules run in order, each through `_delete_in_chunks` so a cleanup never holds one large
delete or materialises every matching id at once, followed by an optional SQLite vacuum. Called from
the `errors_cleanup` management command, the opportunistic writer hook (`writer._maybe_cleanup`) and
the `admin_errors.cleanup` Celery task — all three just call `run_cleanup()`.
"""

from __future__ import annotations

import dataclasses
import datetime
import time
from typing import Any

from django.db import connections
from django.db.models import Q, QuerySet
from django.utils import timezone

from admin_errors.conf import settings as conf
from admin_errors.models import Event, Issue, IssueDailyCount

CHUNK_SIZE = 1000
HYSTERESIS = 0.9


@dataclasses.dataclass
class CleanupReport:
    alias: str
    dry_run: bool = False
    events: int = 0
    daily_counts: int = 0
    resolved_issues: int = 0
    ignored_issues: int = 0
    open_issues: int = 0
    evicted_issues: int = 0
    vacuum: str | None = None
    duration_seconds: float = 0.0

    def rows(self) -> list[tuple[str, int]]:
        return [
            ("events", self.events),
            ("daily_counts", self.daily_counts),
            ("resolved_issues", self.resolved_issues),
            ("ignored_issues", self.ignored_issues),
            ("open_issues", self.open_issues),
            ("evicted_issues", self.evicted_issues),
        ]


def _delete_in_chunks(queryset: QuerySet[Any], model: type, *, dry_run: bool) -> int:
    if dry_run:
        return queryset.count()
    alias = queryset.db
    deleted_total = 0
    while True:
        ids = list(queryset.order_by().values_list("pk", flat=True)[:CHUNK_SIZE])
        if not ids:
            break
        _, deleted_per_model = model.objects.using(alias).filter(pk__in=ids).delete()
        deleted_total += deleted_per_model.get(model._meta.label, 0)
    return deleted_total


def _evict_status(alias: str, status: str, remaining: int) -> int:
    # `remaining` can be arbitrarily large, so ids are picked and deleted CHUNK_SIZE at a time
    # rather than materialising one unbounded id list, which would blow past the backend's bind
    # parameter limit (SQLite: 999-32766 depending on version; PostgreSQL: 65535).
    deleted_total = 0
    while remaining > 0:
        ids = list(
            Issue.objects.using(alias)
            .filter(status=status)
            .order_by("last_seen", "pk")
            .values_list("pk", flat=True)[: min(remaining, CHUNK_SIZE)]
        )
        if not ids:
            break
        _, deleted_per_model = Issue.objects.using(alias).filter(pk__in=ids).delete()
        deleted_total += deleted_per_model.get(Issue._meta.label, 0)
        remaining -= len(ids)
    return deleted_total


def _vacuum(alias: str, *, vacuum: bool) -> str | None:
    if connections[alias].vendor != "sqlite":
        return None
    with connections[alias].cursor() as cursor:
        if vacuum:
            cursor.execute("VACUUM")
            return "full"
        if conf.SQLITE_VACUUM == "incremental":
            cursor.execute("PRAGMA auto_vacuum")
            row = cursor.fetchone()
            if row and row[0] == 2:
                cursor.execute("PRAGMA incremental_vacuum")
                return "incremental"
        return None


def run_cleanup(
    *, using: str | None = None, vacuum: bool = False, dry_run: bool = False
) -> CleanupReport:
    alias = using or conf.DATABASE
    report = CleanupReport(alias=alias, dry_run=dry_run)
    started = time.monotonic()
    # One `now` for every rule, so a cleanup cannot straddle a clock tick.
    now = timezone.now()

    # Rule 1: events older than EVENT_RETENTION_DAYS.
    event_cutoff = now - datetime.timedelta(days=conf.EVENT_RETENTION_DAYS)
    events_qs = Event.objects.using(alias).filter(timestamp__lt=event_cutoff)
    report.events = _delete_in_chunks(events_qs, Event, dry_run=dry_run)

    # Rule 2: daily counts older than DAILY_COUNT_RETENTION_DAYS.
    daily_cutoff = (now - datetime.timedelta(days=conf.DAILY_COUNT_RETENTION_DAYS)).date()
    daily_qs = IssueDailyCount.objects.using(alias).filter(date__lt=daily_cutoff)
    report.daily_counts = _delete_in_chunks(daily_qs, IssueDailyCount, dry_run=dry_run)

    # Rule 3: resolved issues past RESOLVED_ISSUE_TTL_DAYS, i.e. max(resolved_at, last_seen) <
    # cutoff, expressed without Greatest() (portable, NULL-safe: resolved_at IS NULL counts as
    # "no later than last_seen" rather than excluding the row).
    resolved_cutoff = now - datetime.timedelta(days=conf.RESOLVED_ISSUE_TTL_DAYS)
    resolved_qs = Issue.objects.using(alias).filter(
        Q(resolved_at__lt=resolved_cutoff) | Q(resolved_at__isnull=True),
        status=Issue.Status.RESOLVED,
        last_seen__lt=resolved_cutoff,
    )
    report.resolved_issues = _delete_in_chunks(resolved_qs, Issue, dry_run=dry_run)

    # Rule 4: ignored issues past IGNORED_ISSUE_TTL_DAYS, skipped entirely when the TTL is None.
    if conf.IGNORED_ISSUE_TTL_DAYS is not None:
        ignored_cutoff = now - datetime.timedelta(days=conf.IGNORED_ISSUE_TTL_DAYS)
        ignored_qs = Issue.objects.using(alias).filter(
            status=Issue.Status.IGNORED, last_seen__lt=ignored_cutoff
        )
        report.ignored_issues = _delete_in_chunks(ignored_qs, Issue, dry_run=dry_run)

    # Rule 5: open issues past OPEN_ISSUE_TTL_DAYS.
    open_cutoff = now - datetime.timedelta(days=conf.OPEN_ISSUE_TTL_DAYS)
    open_qs = Issue.objects.using(alias).filter(status=Issue.Status.OPEN, last_seen__lt=open_cutoff)
    report.open_issues = _delete_in_chunks(open_qs, Issue, dry_run=dry_run)

    # Rule 6: eviction against MAX_ISSUES, with hysteresis, walking ignored -> resolved -> open.
    if conf.MAX_ISSUES is not None:
        total = Issue.objects.using(alias).count()
        target = int(conf.MAX_ISSUES * HYSTERESIS)
        if dry_run:
            # Nothing was actually deleted by rules 3-5 above, so the eviction estimate has to be
            # computed arithmetically from what they *would* have deleted.
            would_delete = report.resolved_issues + report.ignored_issues + report.open_issues
            if total - would_delete > conf.MAX_ISSUES:
                report.evicted_issues = max(0, total - would_delete - target)
        elif total > conf.MAX_ISSUES:
            remaining = total - target
            evicted = 0
            for status in (Issue.Status.IGNORED, Issue.Status.RESOLVED, Issue.Status.OPEN):
                if remaining <= 0:
                    break
                deleted = _evict_status(alias, status, remaining)
                evicted += deleted
                remaining -= deleted
            report.evicted_issues = evicted

    # Rule 7: vacuum, SQLite only, never under dry_run.
    if not dry_run:
        report.vacuum = _vacuum(alias, vacuum=vacuum)

    report.duration_seconds = time.monotonic() - started
    return report
