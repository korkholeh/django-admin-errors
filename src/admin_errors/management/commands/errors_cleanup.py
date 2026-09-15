"""`manage.py errors_cleanup`: run the retention rules on demand (spec section 9.3)."""

from __future__ import annotations

from typing import Any

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from admin_errors import retention
from admin_errors.conf import settings as conf


class Command(BaseCommand):
    help = "Delete expired admin_errors rows and optionally VACUUM SQLite (spec section 9.3)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--vacuum", action="store_true", help="Also run a full VACUUM (SQLite only)."
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting anything.",
        )
        parser.add_argument(
            "--database",
            default=None,
            help="Database alias to clean (default: ADMIN_ERRORS['DATABASE']).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        alias = options["database"] or conf.DATABASE
        if alias not in django_settings.DATABASES:
            raise CommandError(
                f"Unknown database alias {alias!r}. Configured aliases: "
                f"{sorted(django_settings.DATABASES)}."
            )

        if options["vacuum"] and not options["dry_run"]:
            self.stderr.write(
                self.style.WARNING(
                    "--vacuum runs a full VACUUM: it rewrites the whole database file and holds "
                    "an exclusive lock until it finishes. Avoid running it under load."
                )
            )

        report = retention.run_cleanup(
            using=alias, vacuum=options["vacuum"], dry_run=options["dry_run"]
        )

        if options["dry_run"]:
            self.stdout.write("(dry run — nothing deleted)")
        for label, count in report.rows():
            self.stdout.write(f"{label}: {count}")
        if options["vacuum"] and options["dry_run"]:
            self.stdout.write("vacuum: skipped (dry run)")
        else:
            self.stdout.write(f"vacuum: {report.vacuum or 'none'}")
        self.stdout.write(f"duration_seconds: {report.duration_seconds:.3f}")
