"""`manage.py demo_seed --issues N --days D [--reset]` (spec section 13).

Generates realistic-looking data for screenshots and manual QA through the *real* capture
pipeline (forced synchronous, admission limiter off) so the shipped fingerprint algorithm is what
produces the grouping — no explicit `fingerprint=` override anywhere in this module. Everything
after the initial capture (timestamps, counts, daily counts, status) is deterministic:
`random.Random(20260915)` and a fixed exception/culprit/word list make `--reset` idempotent.
"""

from __future__ import annotations

import datetime
import random
from typing import Any

from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.test.utils import override_settings
from django.utils import timezone

from admin_errors import api
from admin_errors.conf import settings as conf
from admin_errors.models import Event, Issue, IssueDailyCount

_SEED = 20260915

# 8 x 8 = 64 unique (exception, culprit) pairs — enough headroom above the default 40 issues and
# the admission-limiter test's 60. Each culprit function has a distinct qualname, which is what
# `context.select_culprit` reports, so the pair alone makes every fingerprint distinct regardless
# of the message.
_EXCEPTIONS = [
    ValueError,
    TypeError,
    KeyError,
    RuntimeError,
    ConnectionError,
    TimeoutError,
    AttributeError,
    IndexError,
]

_VERBS = ["process", "validate", "fetch", "parse", "sync", "commit", "render", "authenticate"]
_NOUNS = ["payment", "order", "invoice", "session", "upload", "webhook", "report", "cache"]


def _process_payment(exc_cls: type[Exception], message: str) -> None:
    raise exc_cls(message)


def _validate_order(exc_cls: type[Exception], message: str) -> None:
    raise exc_cls(message)


def _fetch_invoice(exc_cls: type[Exception], message: str) -> None:
    raise exc_cls(message)


def _parse_webhook(exc_cls: type[Exception], message: str) -> None:
    raise exc_cls(message)


def _sync_cache(exc_cls: type[Exception], message: str) -> None:
    raise exc_cls(message)


def _commit_transaction(exc_cls: type[Exception], message: str) -> None:
    raise exc_cls(message)


def _render_report(exc_cls: type[Exception], message: str) -> None:
    raise exc_cls(message)


def _authenticate_session(exc_cls: type[Exception], message: str) -> None:
    raise exc_cls(message)


_CULPRITS = [
    _process_payment,
    _validate_order,
    _fetch_invoice,
    _parse_webhook,
    _sync_cache,
    _commit_transaction,
    _render_report,
    _authenticate_session,
]


class Command(BaseCommand):
    help = "Seed the demo database with realistic issues through the real capture pipeline."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--issues", type=int, default=40, help="Number of issues to create.")
        parser.add_argument("--days", type=int, default=30, help="Spread first_seen over N days.")
        parser.add_argument(
            "--reset", action="store_true", help="Delete existing admin_errors rows first."
        )
        parser.add_argument(
            "--backfill-history",
            type=int,
            default=None,
            metavar="ISSUE_ID",
            help=(
                "Give one existing issue a --days history of daily counts instead of seeding "
                "new ones. Its events, payload and status are left alone, so an issue captured "
                "from a real request keeps its request context and traceback while gaining the "
                "occurrence chart a day-old issue cannot have."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        n_issues = options["issues"]
        n_days = options["days"]
        alias = conf.DATABASE

        if options["backfill_history"] is not None:
            self._backfill_history(options["backfill_history"], n_days, alias)
            return

        max_issues = len(_EXCEPTIONS) * len(_CULPRITS)
        if n_issues > max_issues:
            raise CommandError(
                f"--issues {n_issues} exceeds the {max_issues} distinct (exception, culprit) "
                "pairs this command can fingerprint uniquely; pass a smaller --issues."
            )

        if options["reset"]:
            Event.objects.using(alias).all().delete()
            IssueDailyCount.objects.using(alias).all().delete()
            Issue.objects.using(alias).all().delete()

        before = Issue.objects.using(alias).count()
        rng = random.Random(_SEED)
        now = timezone.now()
        today = now.date()

        merged = {
            **django_settings.ADMIN_ERRORS,
            "TRANSPORT": "sync",
            "NEW_ISSUES_PER_MINUTE": None,
            "EVENT_SAMPLE_PER_HOUR": 50,
        }
        with override_settings(ADMIN_ERRORS=merged):
            for i in range(n_issues):
                self._seed_one(i, n_days, rng, now, today, alias)

        self._ensure_superuser()
        self._ensure_viewer()
        created = Issue.objects.using(alias).count() - before
        self.stdout.write(self.style.SUCCESS(f"seeded {created} issues"))

    def _seed_one(
        self,
        i: int,
        n_days: int,
        rng: random.Random,
        now: datetime.datetime,
        today: datetime.date,
        alias: str,
    ) -> None:
        exc_cls = _EXCEPTIONS[i % len(_EXCEPTIONS)]
        culprit_fn = _CULPRITS[(i // len(_EXCEPTIONS)) % len(_CULPRITS)]
        verb = _VERBS[i % len(_VERBS)]
        noun = _NOUNS[(i * 5) % len(_NOUNS)]
        message = f"{verb} {noun} failed"

        occurrences = rng.randint(1, 4)
        fp = None
        for _ in range(occurrences):
            try:
                culprit_fn(exc_cls, message)
            except exc_cls:
                fp = api.capture_exception()
        if fp is None:
            return  # pragma: no cover - defensive: sync transport never drops here

        issue = Issue.objects.using(alias).get(fingerprint=fp)

        days_ago = rng.randint(0, max(n_days - 1, 0))
        first_seen = timezone.make_aware(
            datetime.datetime.combine(today - datetime.timedelta(days=days_ago), datetime.time())
        ) + datetime.timedelta(hours=rng.randint(0, 23), minutes=rng.randint(0, 59))
        first_seen = min(first_seen, now)
        span_seconds = max(int((now - first_seen).total_seconds()), 0)
        last_seen = first_seen + datetime.timedelta(seconds=rng.randint(0, span_seconds))

        daily_counts = self._random_walk_daily_counts(rng, first_seen.date(), last_seen.date())
        IssueDailyCount.objects.using(alias).filter(issue=issue).delete()
        IssueDailyCount.objects.using(alias).bulk_create(
            IssueDailyCount(issue=issue, date=date, count=count)
            for date, count in daily_counts.items()
        )
        total_count = sum(daily_counts.values())

        events = list(Event.objects.using(alias).filter(issue=issue))
        for event in events:
            offset = rng.randint(0, span_seconds)
            event.timestamp = first_seen + datetime.timedelta(seconds=offset)
        Event.objects.using(alias).bulk_update(events, ["timestamp"])

        issue.first_seen = first_seen
        issue.last_seen = last_seen
        issue.count = total_count
        issue.status, issue.resolved_at = self._status_for(i, rng, first_seen, last_seen)
        issue.save(using=alias)

    def _backfill_history(self, issue_id: int, n_days: int, alias: str) -> None:
        """Give `issue_id` a `n_days` history of daily counts, leaving everything else intact.

        `_seed_one` fabricates a whole issue; this only fabricates the part of one that time
        alone can produce. `last_seen`, the stored events and the payload behind them are left
        untouched, so an issue captured from a real request keeps its traceback, its scrubbed
        request block and its place at the top of a `-last_seen` list while its occurrence chart
        stops being the single bar a minutes-old issue can draw.
        """
        try:
            issue = Issue.objects.using(alias).get(pk=issue_id)
        except Issue.DoesNotExist:
            raise CommandError(f"no issue with id {issue_id}") from None

        rng = random.Random(_SEED)
        last_date = issue.last_seen.date()
        first_seen = timezone.make_aware(
            datetime.datetime.combine(
                last_date - datetime.timedelta(days=max(n_days - 1, 0)), datetime.time()
            )
        )

        daily_counts = self._random_walk_daily_counts(rng, first_seen.date(), last_date)
        IssueDailyCount.objects.using(alias).filter(issue=issue).delete()
        IssueDailyCount.objects.using(alias).bulk_create(
            IssueDailyCount(issue=issue, date=date, count=count)
            for date, count in daily_counts.items()
        )

        issue.first_seen = first_seen
        issue.count = sum(daily_counts.values())
        issue.save(using=alias)
        self.stdout.write(
            f"backfilled {len(daily_counts)} day(s) onto issue {issue_id} "
            f"({issue.exception_type}), count now {issue.count}"
        )

    def _random_walk_daily_counts(
        self, rng: random.Random, start: datetime.date, end: datetime.date
    ) -> dict[datetime.date, int]:
        counts: dict[datetime.date, int] = {}
        level = rng.randint(1, 10)
        date = start
        while date <= end:
            level = max(1, level + rng.randint(-2, 3))
            counts[date] = level
            date += datetime.timedelta(days=1)
        return counts

    def _status_for(
        self,
        i: int,
        rng: random.Random,
        first_seen: datetime.datetime,
        last_seen: datetime.datetime,
    ) -> tuple[str, datetime.datetime | None]:
        position = i + 1
        if position % 7 == 0:
            resolved_at = last_seen + datetime.timedelta(hours=rng.randint(1, 48))
            return Issue.Status.RESOLVED, resolved_at
        if position % 11 == 0:
            return Issue.Status.IGNORED, None
        if position % 13 == 0:
            # "Regressed": resolved once, then occurred again after — status is back to open,
            # but `resolved_at` stays in the past relative to `last_seen`.
            span = max(int((last_seen - first_seen).total_seconds()), 1)
            resolved_at = first_seen + datetime.timedelta(seconds=rng.randint(1, span))
            return Issue.Status.OPEN, resolved_at
        return Issue.Status.OPEN, None

    def _ensure_superuser(self) -> None:
        user_model = get_user_model()
        user, _created = user_model.objects.get_or_create(
            username="admin",
            defaults={"email": "admin@example.com"},
        )
        user.email = user.email or "admin@example.com"
        user.is_staff = True
        user.is_superuser = True
        user.set_password("admin")
        user.save()

    def _ensure_viewer(self) -> None:
        """Staff user `viewer`/`viewer` holding only `view_issue` (spec section 12.5, e2e).

        Reaches the `view_issue`-only permission case from a real browser: the list and detail
        pages render but the request/locals sections are hidden, and a status POST is 403.
        """
        from django.contrib.auth.models import Permission

        user_model = get_user_model()
        user, _created = user_model.objects.get_or_create(
            username="viewer",
            defaults={"email": "viewer@example.com"},
        )
        user.email = user.email or "viewer@example.com"
        user.is_staff = True
        user.is_superuser = False
        user.set_password("viewer")
        user.save()
        view_issue = Permission.objects.get(
            content_type__app_label="admin_errors", codename="view_issue"
        )
        user.user_permissions.set([view_issue])
        self.stdout.write(self.style.SUCCESS("ensured viewer user (view_issue only)"))
