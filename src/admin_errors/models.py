from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Issue(models.Model):
    class Level(models.TextChoices):
        CRITICAL = "critical", _("Critical")
        ERROR = "error", _("Error")
        WARNING = "warning", _("Warning")
        INFO = "info", _("Info")

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        RESOLVED = "resolved", _("Resolved")
        IGNORED = "ignored", _("Ignored")

    fingerprint = models.CharField(_("fingerprint"), max_length=40, unique=True)
    exception_type = models.CharField(_("exception type"), max_length=200, db_index=True)
    title = models.CharField(_("title"), max_length=500)
    culprit = models.CharField(_("culprit"), max_length=500, blank=True)
    level = models.CharField(_("level"), max_length=10, choices=Level.choices, default=Level.ERROR)
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    first_seen = models.DateTimeField(_("first seen"))
    last_seen = models.DateTimeField(_("last seen"), db_index=True)
    count = models.PositiveBigIntegerField(_("count"), default=0)
    resolved_at = models.DateTimeField(_("resolved at"), null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("resolved by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        # "+": no reverse accessor. Prevents a User.issue_set clash with any host app that has its
        # own Issue model FK'd to the user model without an explicit related_name.
        related_name="+",
    )
    notified_at = models.DateTimeField(_("notified at"), null=True, blank=True)
    last_event = models.JSONField(_("last event"), null=True, blank=True)

    class Meta:
        ordering = ["-last_seen"]
        indexes = [
            models.Index(fields=["status", "-last_seen"], name="ae_issue_status_seen_idx"),
        ]
        permissions = [
            ("view_issue_context", "Can view request context and local variables"),
        ]
        verbose_name = _("issue")
        verbose_name_plural = _("issues")

    def __str__(self) -> str:
        return f"{self.exception_type}: {self.title}"


class Event(models.Model):
    issue = models.ForeignKey(
        Issue, verbose_name=_("issue"), on_delete=models.CASCADE, related_name="events"
    )
    timestamp = models.DateTimeField(_("timestamp"), db_index=True)
    payload = models.JSONField(_("payload"))

    class Meta:
        indexes = [
            models.Index(fields=["issue", "-timestamp"], name="ae_event_issue_ts_idx"),
        ]
        verbose_name = _("event")
        verbose_name_plural = _("events")

    def __str__(self) -> str:
        return f"Event({self.issue_id}, {self.timestamp})"


class IssueDailyCount(models.Model):
    issue = models.ForeignKey(
        Issue, verbose_name=_("issue"), on_delete=models.CASCADE, related_name="daily_counts"
    )
    date = models.DateField(_("date"))
    count = models.PositiveIntegerField(_("count"), default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["issue", "date"], name="ae_daily_issue_date_uniq"),
        ]
        indexes = [
            models.Index(fields=["date"], name="ae_daily_date_idx"),
        ]
        verbose_name = _("issue daily count")
        verbose_name_plural = _("issue daily counts")

    def __str__(self) -> str:
        return f"IssueDailyCount({self.issue_id}, {self.date}, {self.count})"
