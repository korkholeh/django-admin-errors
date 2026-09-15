"""`manage.py errors_test`: capture one exception through the real pipeline (risk 12).

Exercises `api.capture_exception()` and `api.flush()` exactly as a host application would, so every
guard, limiter, transport and the writer thread are on the path — the "is it actually wired up?"
instrument the spec calls for.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings as django_settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import NoReverseMatch, reverse

from admin_errors import api
from admin_errors.conf import settings as conf
from admin_errors.models import Issue


class AdminErrorsTestError(Exception):
    pass


class Command(BaseCommand):
    help = "Capture one deliberate test exception and print the resulting issue's admin URL."

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            raise AdminErrorsTestError(
                "admin_errors errors_test: this is a deliberate, harmless test error."
            )
        except AdminErrorsTestError as exc:
            fingerprint = api.capture_exception(exc)
        api.flush()

        issue = None
        if fingerprint is not None:
            issue = Issue.objects.using(conf.DATABASE).filter(fingerprint=fingerprint).first()
        if issue is None:
            raise CommandError(self._diagnosis())

        self.stdout.write(self.style.SUCCESS(f"Captured issue #{issue.pk}: {issue.title}"))
        self.stdout.write(self._admin_url(issue.pk))

    def _diagnosis(self) -> str:
        reasons = []
        if not conf.ENABLED:
            reasons.append("ADMIN_ERRORS['ENABLED'] is False")
        if django_settings.DEBUG and not conf.CAPTURE_IN_DEBUG:
            reasons.append("DEBUG=True and ADMIN_ERRORS['CAPTURE_IN_DEBUG'] is False")
        if conf.BEFORE_SEND:
            reasons.append("ADMIN_ERRORS['BEFORE_SEND'] may have dropped the event")
        if conf.NEW_ISSUES_PER_MINUTE is not None and conf.NEW_ISSUES_PER_MINUTE <= 0:
            reasons.append("the ADMIN_ERRORS['NEW_ISSUES_PER_MINUTE'] admission limit is 0")
        if not reasons:
            reasons.append(
                "no obvious cause found in ADMIN_ERRORS settings; check the "
                "ADMIN_ERRORS['NEW_ISSUES_PER_MINUTE'] admission limit and the writer/transport "
                "configuration"
            )
        return "No issue was captured: " + "; ".join(reasons) + "."

    def _admin_url(self, pk: int) -> str:
        try:
            return reverse("admin:admin_errors_issue_change", args=[pk])
        except NoReverseMatch:
            pass
        try:
            return reverse("admin:index") + f"admin_errors/issue/{pk}/change/"
        except NoReverseMatch:
            return "(admin site not mounted)"
