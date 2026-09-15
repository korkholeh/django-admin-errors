"""Registers `IssueAdmin` (spec section 12). Imported by Django's `admin.autodiscover()`.

Resolution of `ADMIN_SITE` (and the registration it triggers) runs at *module import time*, not
inside `AppConfig.ready()` — CLAUDE.md forbids DB/filesystem/thread work in `ready()`, and
`admin.autodiscover()` is the framework's own mechanism for importing `admin.py` modules, so this
module is never imported before Django itself is ready to have models registered on a site.
"""

from __future__ import annotations

import copy
import datetime
from typing import Any

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch, QuerySet, Sum
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.module_loading import import_string
from django.utils.timesince import timesince
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from admin_errors import signals
from admin_errors.conf import settings as conf
from admin_errors.models import Event, Issue, IssueDailyCount
from admin_errors.templatetags.admin_errors_tags import (
    ae_compact_number,
    ae_sparkline,
    ae_status_badge,
)

_STATUS_FOR_ACTION = {
    "resolve": Issue.Status.RESOLVED,
    "ignore": Issue.Status.IGNORED,
    "reopen": Issue.Status.OPEN,
}

# Keys of the "request" payload block that require `view_issue_context` — spec section 12.5:
# "method + path" stay visible with only `view_issue`.
_GATED_REQUEST_KEYS = {"url", "query", "post", "headers", "cookies", "remote_addr", "user", "body"}


def _apply_status(issue: Issue, action: str, user: Any) -> None:
    """One `UPDATE`, idempotent, firing `issue_status_changed`. Backs both bulk and single."""
    alias = conf.DATABASE
    new_status = _STATUS_FOR_ACTION[action]
    old_status = issue.status
    fields: dict[str, Any] = {"status": new_status}
    if action == "resolve":
        fields["resolved_at"] = timezone.now()
        fields["resolved_by"] = user if getattr(user, "pk", None) else None
    # "reopen" deliberately leaves `resolved_at` untouched so the regressed badge survives.
    Issue.objects.using(alias).filter(pk=issue.pk).update(**fields)
    issue.status = new_status
    issue.resolved_at = fields.get("resolved_at", issue.resolved_at)
    issue.resolved_by = fields.get("resolved_by", issue.resolved_by)
    if old_status != new_status:
        signals.send_safely(
            signals.issue_status_changed,
            issue=issue,
            old_status=old_status,
            new_status=new_status,
            user=user,
        )


def _redact_payload(
    payload: dict[str, Any] | None, can_view_context: bool
) -> dict[str, Any] | None:
    """View-level half of the double permission gate (ADR 0007, risk #2).

    Strips sensitive keys from the payload *before* it ever reaches the template, so a missing
    `{% if %}` in an include cannot leak them — the template-level `{% if perms... %}` checks are a
    second, independent line of defense, not the only one.
    """
    if payload is None or can_view_context:
        return payload
    redacted = copy.deepcopy(payload)
    request_block = redacted.get("request")
    if request_block:
        redacted["request"] = {
            key: value for key, value in request_block.items() if key not in _GATED_REQUEST_KEYS
        }
    redacted.pop("celery", None)
    redacted.pop("extra", None)
    exception = redacted.get("exception")
    if exception:
        for entry in exception.get("chain", []):
            for frame in entry.get("frames", []):
                frame.pop("vars", None)
    for frame in redacted.get("frames", []):
        frame.pop("vars", None)
    return redacted


class LastSeenFilter(admin.SimpleListFilter):
    title = _("last seen")
    parameter_name = "seen"

    _WINDOWS = {
        "1h": datetime.timedelta(hours=1),
        "24h": datetime.timedelta(hours=24),
        "7d": datetime.timedelta(days=7),
        "30d": datetime.timedelta(days=30),
    }

    def lookups(self, request: HttpRequest, model_admin: admin.ModelAdmin) -> list[tuple[str, Any]]:
        return [
            ("1h", _("Last hour")),
            ("24h", _("Last 24 hours")),
            ("7d", _("Last 7 days")),
            ("30d", _("Last 30 days")),
        ]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        delta = self._WINDOWS.get(self.value())
        if delta is None:
            return queryset
        return queryset.filter(last_seen__gte=timezone.now() - delta)


class IssueAdmin(admin.ModelAdmin):
    change_list_template = "admin/admin_errors/issue/change_list.html"
    change_form_template = "admin/admin_errors/issue/change_form.html"

    list_display = [
        "issue_cell",
        "count_display",
        "last_seen_display",
        "first_seen_display",
        "status_badge",
        "trend",
    ]
    list_display_links = None
    list_filter = ["status", "level", "exception_type", LastSeenFilter]
    search_fields = ["title", "exception_type", "culprit"]
    ordering = ["-last_seen"]
    list_per_page = 50
    date_hierarchy = None
    actions = ["resolve", "ignore", "reopen"]

    # -- Permissions -----------------------------------------------------------------------

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def get_readonly_fields(self, request: HttpRequest, obj: Issue | None = None) -> list[str]:
        return [field.name for field in self.model._meta.concrete_fields]

    # -- Assets ------------------------------------------------------------------------------

    @property
    def media(self) -> forms.Media:
        return super().media + forms.Media(
            css={"all": ("admin_errors/admin_errors.css",)},
            js=("admin_errors/admin_errors.js",),
        )

    # -- Queryset ------------------------------------------------------------------------------

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        today = timezone.now().astimezone(datetime.timezone.utc).date()
        cutoff = today - datetime.timedelta(days=13)
        return Issue.objects.using(conf.DATABASE).prefetch_related(
            Prefetch(
                "daily_counts",
                queryset=IssueDailyCount.objects.using(conf.DATABASE)
                .filter(date__gte=cutoff)
                .order_by("date"),
                to_attr="recent_counts",
            )
        )

    # -- List display columns -------------------------------------------------------------------

    @admin.display(description=_("Issue"))
    def issue_cell(self, obj: Issue) -> str:
        url = reverse("admin:admin_errors_issue_change", args=[obj.pk])
        return format_html(
            '<a href="{}" class="ae-issue-cell">'
            '<strong class="ae-issue-type">{}</strong>'
            '<span class="ae-issue-title">{}</span>'
            '<span class="ae-issue-culprit">{}</span></a>',
            url,
            obj.exception_type,
            obj.title,
            obj.culprit,
        )

    @admin.display(description=_("Count"), ordering="count")
    def count_display(self, obj: Issue) -> str:
        return ae_compact_number(obj.count)

    @admin.display(description=_("Last seen"), ordering="last_seen")
    def last_seen_display(self, obj: Issue) -> str:
        return format_html(
            '<span title="{}">{}</span>', obj.last_seen.isoformat(), timesince(obj.last_seen)
        )

    @admin.display(description=_("First seen"), ordering="first_seen")
    def first_seen_display(self, obj: Issue) -> str:
        return format_html(
            '<span title="{}">{}</span>', obj.first_seen.isoformat(), timesince(obj.first_seen)
        )

    @admin.display(description=_("Status"))
    def status_badge(self, obj: Issue) -> str:
        return ae_status_badge(obj)

    @admin.display(description=_("Trend"))
    def trend(self, obj: Issue) -> str:
        return ae_sparkline(obj)

    # -- List view: summary cards ---------------------------------------------------------------

    def changelist_view(self, request: HttpRequest, extra_context: dict[str, Any] | None = None):
        extra_context = extra_context or {}
        if request.user.has_perm("admin_errors.view_issue"):
            extra_context["ae_cards"] = self._summary_cards()
        return super().changelist_view(request, extra_context=extra_context)

    def _summary_cards(self) -> dict[str, int]:
        alias = conf.DATABASE
        unresolved = Issue.objects.using(alias).filter(status=Issue.Status.OPEN).count()
        today = timezone.now().astimezone(datetime.timezone.utc).date()
        yesterday = today - datetime.timedelta(days=1)
        events_24h = (
            IssueDailyCount.objects.using(alias)
            .filter(date__in=[today, yesterday])
            .aggregate(total=Sum("count"))["total"]
            or 0
        )
        new_24h = (
            Issue.objects.using(alias)
            .filter(first_seen__gte=timezone.now() - datetime.timedelta(hours=24))
            .count()
        )
        return {"unresolved": unresolved, "events_24h": events_24h, "new_24h": new_24h}

    # -- Detail view -----------------------------------------------------------------------------

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ):
        extra_context = extra_context or {}
        issue = get_object_or_404(self.get_queryset(request), pk=object_id)
        can_view_context = request.user.has_perm("admin_errors.view_issue_context")

        extra_context["ae_payload"] = _redact_payload(issue.last_event, can_view_context)
        events = list(
            issue.events.using(conf.DATABASE).order_by("-timestamp")[: conf.EVENTS_PER_ISSUE]
        )
        for event in events:
            event.ae_payload = _redact_payload(event.payload, can_view_context)
        extra_context["ae_events"] = events
        today = timezone.now().astimezone(datetime.timezone.utc).date()
        thirty_days_ago = today - datetime.timedelta(days=29)
        extra_context["ae_daily_counts"] = list(
            issue.daily_counts.using(conf.DATABASE)
            .filter(date__gte=thirty_days_ago)
            .order_by("date")
        )
        extra_context["ae_can_view_context"] = can_view_context
        extra_context["ae_can_change"] = request.user.has_perm("admin_errors.change_issue")
        extra_context["ae_can_delete"] = self.has_delete_permission(request, issue)
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def render_change_form(
        self,
        request: HttpRequest,
        context: dict[str, Any],
        add=False,
        change=False,
        form_url="",
        obj=None,
    ):
        context.update(
            {
                "show_save": False,
                "show_save_and_continue": False,
                "show_save_and_add_another": False,
            }
        )
        return super().render_change_form(
            request, context, add=add, change=change, form_url=form_url, obj=obj
        )

    # -- Custom URLs: status transitions + event detail -------------------------------------------

    def get_urls(self):
        custom = [
            path(
                "<int:object_id>/status/<str:action>/",
                self.admin_site.admin_view(require_POST(self.status_view)),
                name="admin_errors_issue_status",
            ),
            path(
                "<int:object_id>/events/<int:event_id>/",
                self.admin_site.admin_view(self.event_detail_view),
                name="admin_errors_issue_event",
            ),
        ]
        return custom + super().get_urls()

    def status_view(self, request: HttpRequest, object_id: int, action: str) -> HttpResponse:
        if not request.user.has_perm("admin_errors.change_issue"):
            raise PermissionDenied
        if action not in _STATUS_FOR_ACTION:
            raise Http404(f"unknown status action {action!r}")
        issue = get_object_or_404(self.get_queryset(request), pk=object_id)
        _apply_status(issue, action, request.user)
        messages.success(request, _("Issue status updated."))
        return redirect(reverse("admin:admin_errors_issue_change", args=[issue.pk]))

    def event_detail_view(
        self, request: HttpRequest, object_id: int, event_id: int
    ) -> HttpResponse:
        if not request.user.has_perm("admin_errors.view_issue"):
            raise PermissionDenied
        issue = get_object_or_404(self.get_queryset(request), pk=object_id)
        event = get_object_or_404(Event.objects.using(conf.DATABASE), pk=event_id, issue=issue)
        can_view_context = request.user.has_perm("admin_errors.view_issue_context")
        context = {
            **self.admin_site.each_context(request),
            "title": event,
            "original": issue,
            "issue": issue,
            "event": event,
            "opts": self.model._meta,
            "ae_payload": _redact_payload(event.payload, can_view_context),
            "ae_can_view_context": can_view_context,
            "ae_can_change": request.user.has_perm("admin_errors.change_issue"),
            "ae_can_delete": self.has_delete_permission(request, issue),
            "media": self.media,
        }
        return render(request, "admin/admin_errors/issue/event_detail.html", context)

    # -- Bulk actions ------------------------------------------------------------------------------

    @admin.action(description=_("Resolve selected issues"), permissions=["change"])
    def resolve(self, request: HttpRequest, queryset: QuerySet) -> None:
        self._bulk_status(request, queryset, "resolve")

    @admin.action(description=_("Ignore selected issues"), permissions=["change"])
    def ignore(self, request: HttpRequest, queryset: QuerySet) -> None:
        self._bulk_status(request, queryset, "ignore")

    @admin.action(description=_("Reopen selected issues"), permissions=["change"])
    def reopen(self, request: HttpRequest, queryset: QuerySet) -> None:
        self._bulk_status(request, queryset, "reopen")

    def _bulk_status(self, request: HttpRequest, queryset: QuerySet, action: str) -> None:
        count = 0
        for issue in queryset:
            _apply_status(issue, action, request.user)
            count += 1
        self.message_user(request, _("%(count)d issue(s) updated.") % {"count": count})


def register(site: AdminSite) -> None:
    """Public API (ADR 0005): `register(admin.site)` or a host's own `AdminSite`."""
    if Issue in site._registry:
        return
    site.register(Issue, IssueAdmin)


def _resolve_site() -> AdminSite | None:
    admin_site_setting = conf.ADMIN_SITE
    if admin_site_setting is False:
        return None
    if admin_site_setting is None:
        from django.contrib.admin import site as default_site

        return default_site
    resolved = import_string(admin_site_setting)
    if isinstance(resolved, type):
        resolved = resolved()
    return resolved


_site = _resolve_site()
if _site is not None:
    register(_site)
