"""`EmailNotifier`: the default `NOTIFY_BACKEND` (spec section 10).

`refresh_connections()` connects/disconnects the two thin receivers below to `issue_created` /
`issue_regressed` depending on `NOTIFY_BACKEND`, `NOTIFY_ON` and whether there is anyone to notify.
It runs from `AdminErrorsConfig.ready()` (no DB, no thread, no filesystem — only signal wiring) and
from this module's own `setting_changed` receiver, which resets `conf.settings`'s cache first so it
never reads a stale value regardless of receiver order.

A receiver connects only when `NOTIFY_BACKEND` resolves, the matching `reason` is in `NOTIFY_ON`,
and the backend class's optional `is_enabled()` hook returns `True` (a backend with no such hook is
always considered enabled — `EmailNotifier.is_enabled()` is the one that requires a non-empty
recipient list). Connecting dynamically, rather than always connecting one dispatcher that resolves
the backend per call, keeps `storage._fire`'s `has_listeners()` check truthful: with
`NOTIFY_BACKEND=None` or a disabled backend (the default test host), no receiver is attached, so the
create path adds no extra `SELECT` and the `test_storage.py` query budgets do not move.
"""

from __future__ import annotations

import datetime
import socket
from typing import Any

from django.conf import settings as django_settings
from django.core.mail import send_mail
from django.db.models import Q
from django.test.signals import setting_changed
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.module_loading import import_string
from django.utils.translation import gettext

from admin_errors import signals, textformat
from admin_errors.conf import settings as conf
from admin_errors.models import Event, Issue

_CREATED_DISPATCH_UID = "admin_errors.notifications.on_issue_created"
_REGRESSED_DISPATCH_UID = "admin_errors.notifications.on_issue_regressed"
_TRACEBACK_MAX_FRAMES = 10


class EmailNotifier:
    """Default `NOTIFY_BACKEND`: one plain-text email via `django.core.mail.send_mail`."""

    @staticmethod
    def is_enabled() -> bool:
        return bool(_recipients())

    def notify(self, *, issue: Issue, event: Event | None, reason: str) -> bool:
        recipients = _recipients()
        if not recipients:
            return False

        now = timezone.now()
        threshold = now - datetime.timedelta(seconds=conf.NOTIFY_THROTTLE_SECONDS)
        updated = (
            Issue.objects.using(conf.DATABASE)
            .filter(pk=issue.pk)
            .filter(Q(notified_at__isnull=True) | Q(notified_at__lt=threshold))
            .update(notified_at=now)
        )
        if not updated:
            return False

        send_mail(
            _build_subject(issue, reason),
            _build_body(issue, event, reason),
            None,  # -> DEFAULT_FROM_EMAIL
            recipients,
            fail_silently=False,
        )
        return True


def _recipients() -> list[str]:
    configured = conf.NOTIFY_RECIPIENTS
    if configured:
        return list(configured)
    return [email for _name, email in django_settings.ADMINS]


def _prefix() -> str:
    try:
        from django.apps import apps as django_apps

        if django_apps.is_installed("django.contrib.sites"):
            from django.contrib.sites.models import Site

            return Site.objects.get_current().name
    except Exception:
        pass
    try:
        return socket.gethostname()
    except Exception:
        return ""


def _issue_title(issue: Issue) -> str:
    if issue.exception_type:
        if issue.culprit:
            return f"{issue.exception_type} in {issue.culprit}"
        return issue.exception_type
    return issue.title


def _build_subject(issue: Issue, reason: str) -> str:
    label = gettext("Regression:") if reason == "regressed" else gettext("New issue:")
    return f"[{_prefix()}] {label} {_issue_title(issue)}"


def _admin_url(issue: Issue) -> str | None:
    try:
        path = reverse("admin:admin_errors_issue_change", args=[issue.pk])
    except NoReverseMatch:
        return None
    return f"{conf.NOTIFY_BASE_URL}{path}"


def _build_body(issue: Issue, event: Event | None, reason: str) -> str:
    heading_template = (
        gettext("Regression in %(prefix)s")
        if reason == "regressed"
        else gettext("New issue in %(prefix)s")
    )
    heading = heading_template % {"prefix": _prefix()}
    payload = event.payload if event is not None else issue.last_event
    traceback_text = textformat.format_traceback_text(
        payload, include_locals=False, max_frames=_TRACEBACK_MAX_FRAMES
    )
    lines = [
        heading,
        "",
        f"{gettext('Title')}: {issue.title}",
        f"{gettext('Culprit')}: {issue.culprit}",
        f"{gettext('Level')}: {issue.level}",
        f"{gettext('Occurrences')}: {issue.count}",
        f"{gettext('First seen')}: {issue.first_seen.isoformat()}",
        f"{gettext('Last seen')}: {issue.last_seen.isoformat()}",
    ]
    if traceback_text:
        lines += ["", traceback_text]
    admin_url = _admin_url(issue)
    if admin_url:
        lines += ["", admin_url]
    return "\n".join(lines)


def _dispatch(
    reason: str, *, issue: Issue | None = None, event: Event | None = None, **_: Any
) -> None:
    if issue is None:
        return
    backend_cls = import_string(conf.NOTIFY_BACKEND)
    backend_cls().notify(issue=issue, event=event, reason=reason)


def _on_issue_created(sender: Any = None, **kwargs: Any) -> None:
    _dispatch("created", **kwargs)


def _on_issue_regressed(sender: Any = None, **kwargs: Any) -> None:
    _dispatch("regressed", **kwargs)


def refresh_connections() -> None:
    """Connect/disconnect the two receivers to match current settings. Idempotent either way."""
    _sync_connection(
        signals.issue_created, _on_issue_created, _CREATED_DISPATCH_UID, reason="created"
    )
    _sync_connection(
        signals.issue_regressed, _on_issue_regressed, _REGRESSED_DISPATCH_UID, reason="regressed"
    )


def _backend_enabled() -> bool:
    if conf.NOTIFY_BACKEND is None:
        return False
    try:
        backend_cls = import_string(conf.NOTIFY_BACKEND)
    except (ImportError, AttributeError):
        return False
    is_enabled = getattr(backend_cls, "is_enabled", None)
    if is_enabled is None:
        return True
    return bool(is_enabled())


def _sync_connection(signal: Any, receiver: Any, dispatch_uid: str, *, reason: str) -> None:
    should_connect = reason in conf.NOTIFY_ON and _backend_enabled()
    if should_connect:
        signal.connect(receiver, dispatch_uid=dispatch_uid)
    else:
        signal.disconnect(dispatch_uid=dispatch_uid)


def _on_setting_changed(*, sender: Any = None, **kwargs: Any) -> None:
    conf.reset()
    refresh_connections()


setting_changed.connect(
    _on_setting_changed, dispatch_uid="admin_errors.notifications.setting_changed"
)
