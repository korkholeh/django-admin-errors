"""`manage.py errors_stats`: row counts, date range and approximate on-disk size."""

from __future__ import annotations

import dataclasses
from typing import Any

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connections
from django.db.models import Count, Max, Min

from admin_errors import writer
from admin_errors.conf import settings as conf
from admin_errors.models import Event, Issue, IssueDailyCount

_PG_MODELS = (Issue, Event, IssueDailyCount)


class Command(BaseCommand):
    help = "Print row counts, date range and approximate on-disk size for admin_errors."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--database",
            default=None,
            help="Database alias to inspect (default: ADMIN_ERRORS['DATABASE']).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        alias = options["database"] or conf.DATABASE
        if alias not in django_settings.DATABASES:
            raise CommandError(
                f"Unknown database alias {alias!r}. Configured aliases: "
                f"{sorted(django_settings.DATABASES)}."
            )

        issue_qs = Issue.objects.using(alias)
        self.stdout.write(f"issues: {issue_qs.count()}")
        self.stdout.write(f"events: {Event.objects.using(alias).count()}")
        self.stdout.write(f"daily_counts: {IssueDailyCount.objects.using(alias).count()}")

        bounds = issue_qs.aggregate(oldest=Min("first_seen"), newest=Max("last_seen"))
        self.stdout.write(f"oldest issue first_seen: {bounds['oldest'] or 'n/a'}")
        self.stdout.write(f"newest issue last_seen: {bounds['newest'] or 'n/a'}")

        for row in issue_qs.values("status").annotate(count=Count("pk")).order_by("status"):
            self.stdout.write(f"status[{row['status']}]: {row['count']}")

        self.stdout.write(f"approximate size: {self._approximate_size(alias)}")

        self.stdout.write(
            "writer stats (process-local; zero if this management command's own process never "
            "ran the writer thread):"
        )
        for field in dataclasses.fields(writer.stats):
            self.stdout.write(f"  {field.name}: {getattr(writer.stats, field.name)}")

    def _approximate_size(self, alias: str) -> str:
        connection = connections[alias]
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA page_count")
                page_count = cursor.fetchone()[0]
                cursor.execute("PRAGMA page_size")
                page_size = cursor.fetchone()[0]
            return f"{page_count * page_size} bytes"
        if connection.vendor == "postgresql":
            total = 0
            with connection.cursor() as cursor:
                for model in _PG_MODELS:
                    cursor.execute(
                        "SELECT pg_total_relation_size(%s::regclass)", [model._meta.db_table]
                    )
                    total += cursor.fetchone()[0] or 0
            return f"{total} bytes"
        return "n/a"
