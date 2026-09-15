"""Admin UI: registration, template tags, list/detail rendering, status transitions,
permission gating, query budgets and asset budgets (spec section 12, PLAN.md phase 8)."""

from __future__ import annotations

import datetime
import re
from collections import namedtuple

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Permission
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from admin_errors import admin as admin_module
from admin_errors.models import Issue
from admin_errors.templatetags import admin_errors_tags as tags
from tests.conftest import (
    SECRET_COOKIE,
    SECRET_HEADER,
    SECRET_LOCAL,
    SECRET_POST,
    SECRET_USERNAME,
)

pytestmark = pytest.mark.django_db

_DC = namedtuple("_DC", ["date", "count"])


def _grant(user, *codenames):
    perms = Permission.objects.filter(
        content_type__app_label="admin_errors", codename__in=codenames
    )
    assert perms.count() == len(codenames)
    user.user_permissions.set(perms)


def _staff_user(django_user_model, *, perms=(), superuser=False, username="staff"):
    user = django_user_model.objects.create_user(username=username, password="x", is_staff=True)
    if superuser:
        user.is_superuser = True
        user.save()
    elif perms:
        _grant(user, *perms)
    return user


# -- T1: fixtures --------------------------------------------------------------------------------


def test_issue_factory_builds_a_full_payload(issue_factory):
    issue = issue_factory(events=2, days=5)
    assert issue.last_event["exception"]["chain"][0]["cause"] is True
    assert issue.last_event["request"]["headers"]["X-Api-Key"] == SECRET_HEADER
    assert issue.events.count() == 2
    assert issue.daily_counts.count() == 6  # inclusive of both endpoints


# -- T2: registration + skeleton -----------------------------------------------------------------


def test_issue_is_registered_on_the_default_site():
    from django.contrib import admin as django_admin

    assert Issue in django_admin.site._registry
    assert isinstance(django_admin.site._registry[Issue], admin_module.IssueAdmin)


def test_register_on_a_custom_site():
    custom_site = AdminSite(name="custom")
    admin_module.register(custom_site)
    assert Issue in custom_site._registry
    # Idempotent: a second call must not raise AlreadyRegistered.
    admin_module.register(custom_site)


def test_admin_site_false_skips_registration():
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync", "ADMIN_SITE": False}):
        assert admin_module._resolve_site() is None


def test_admin_site_false_skips_default_registration_subprocess():
    """`_resolve_site() is None` alone doesn't prove the default site stays unregistered: the
    default `AdminConfig.ready()` calls `autodiscover()` at `django.setup()` time, which already
    ran (registering Issue on the default site) long before this test process could apply
    `override_settings`. Only a fresh interpreter booted with `ADMIN_SITE=False` in place can show
    the default registration is actually skipped."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    script = (
        "import django; django.setup(); "
        "from django.contrib import admin as django_admin; "
        "from admin_errors.models import Issue; "
        "assert Issue not in django_admin.site._registry, django_admin.site._registry; "
        "print('OK')"
    )
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "tests.settings",
        "ADMIN_ERRORS_TEST_ADMIN_SITE_FALSE": "1",
        "PYTHONPATH": str(repo_root),
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_admin_site_dotted_path_resolves():
    with override_settings(
        ADMIN_ERRORS={
            "TRANSPORT": "sync",
            "ADMIN_SITE": "django.contrib.admin.sites.AdminSite",
        }
    ):
        resolved = admin_module._resolve_site()
    assert isinstance(resolved, AdminSite)


def test_has_add_permission_is_false(rf):
    ma = admin_module.IssueAdmin(Issue, admin_module.admin.site)
    assert ma.has_add_permission(rf.get("/")) is False


def test_all_fields_are_readonly(rf):
    ma = admin_module.IssueAdmin(Issue, admin_module.admin.site)
    expected = {f.name for f in Issue._meta.concrete_fields}
    assert set(ma.get_readonly_fields(rf.get("/"))) == expected


def test_add_view_is_not_reachable(client, django_user_model):
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_add"))
    assert response.status_code == 403


# -- T3: template tags ----------------------------------------------------------------------------


def test_sparkline_zero_fills_missing_days():
    today = timezone.now().astimezone(datetime.timezone.utc).date()
    dates, values = tags._zero_filled_series([_DC(today, 5)], 3, end=today)
    assert dates == [today - datetime.timedelta(days=2), today - datetime.timedelta(days=1), today]
    assert values == [0, 0, 5]


def test_sparkline_handles_all_zero_counts():
    dates = [datetime.date(2026, 1, day) for day in range(1, 4)]
    svg = tags._render_sparkline(dates, [0, 0, 0])
    assert "<svg" in svg
    assert "NaN" not in svg


def test_sparkline_aria_label_states_the_counts():
    dates = [datetime.date(2026, 1, 1), datetime.date(2026, 1, 2)]
    svg = tags._render_sparkline(dates, [3, 7])
    assert 'role="img"' in svg
    assert "aria-hidden" not in svg
    assert "2026-01-01: 3" in svg
    assert "2026-01-02: 7" in svg


def test_bar_chart_renders_30_bars():
    svg = tags._render_bar_chart(
        [datetime.date(2026, 1, day) for day in range(1, 31)], list(range(30))
    )
    assert svg.count("<rect") == 30
    assert "per day (UTC)" in svg


def test_bar_chart_aria_label_states_the_counts():
    dates = [datetime.date(2026, 1, 1), datetime.date(2026, 1, 2)]
    svg = tags._render_bar_chart(dates, [3, 7])
    assert 'role="img"' in svg
    assert "2026-01-01: 3" in svg
    assert "2026-01-02: 7" in svg


def test_status_badge_regressed():
    issue = Issue(status=Issue.Status.OPEN, resolved_at=timezone.now())
    assert "Regressed" in tags.ae_status_badge(issue)


def test_status_badge_resolved_and_ignored():
    resolved = Issue(status=Issue.Status.RESOLVED)
    ignored = Issue(status=Issue.Status.IGNORED)
    assert "ae-badge--resolved" in tags.ae_status_badge(resolved)
    assert "ae-badge--ignored" in tags.ae_status_badge(ignored)


def test_traceback_blocks_order_and_separators(payload_factory):
    payload = payload_factory()
    blocks = tags.ae_traceback_blocks(payload)
    assert [b["exc"]["type"] for b in blocks] == ["ValueError", "RuntimeError"]
    assert blocks[0]["separator"] is None
    assert "direct cause" in str(blocks[1]["separator"])


def test_compact_number():
    assert tags.ae_compact_number(999) == "999"
    assert tags.ae_compact_number(1200) == "1.2k"
    assert tags.ae_compact_number(1_500_000) == "1.5m"


def test_tags_escape_payload_html():
    from django.template import Context, Template

    frame = {"module": "<script>", "function": "f", "filename": "f.py", "lineno": 1}
    rendered = Template("{% load admin_errors_tags %}{{ frame|ae_frame_label }}").render(
        Context({"frame": frame})
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


# -- T4: issue list template + cards ---------------------------------------------------------------


def test_changelist_renders_cards(client, django_user_model, issue_factory):
    issue_factory()
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_changelist"))
    assert response.status_code == 200
    assert "Unresolved issues" in response.content.decode()
    assert "Events last 24h" in response.content.decode()
    assert "New issues last 24h" in response.content.decode()


def test_changelist_renders_badges_and_sparklines(client, django_user_model, issue_factory):
    issue_factory()
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_changelist"))
    body = response.content.decode()
    assert body.count("<svg") >= 1
    assert "ae-badge" in body


def test_changelist_links_each_row_to_the_detail_page(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_changelist"))
    detail_url = reverse("admin:admin_errors_issue_change", args=[issue.pk])
    assert detail_url in response.content.decode()


def test_changelist_empty_state(client, django_user_model):
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_changelist"))
    assert response.status_code == 200


# -- T5: list query budget ---------------------------------------------------------------------


def test_changelist_query_budget(
    client, django_user_model, issue_factory, django_assert_max_num_queries
):
    for _ in range(50):
        issue_factory(events=1, days=14)
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    with django_assert_max_num_queries(12):
        response = client.get(reverse("admin:admin_errors_issue_changelist"))
    assert response.status_code == 200


# -- T6: filters, search, ordering ---------------------------------------------------------------


def test_filter_by_status(client, django_user_model, issue_factory):
    open_issue = issue_factory(status=Issue.Status.OPEN)
    other = issue_factory(status=Issue.Status.RESOLVED)
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_changelist"), {"status": "open"})
    body = response.content.decode()
    assert reverse("admin:admin_errors_issue_change", args=[open_issue.pk]) in body
    assert reverse("admin:admin_errors_issue_change", args=[other.pk]) not in body


def test_filter_by_level(client, django_user_model, issue_factory):
    warning_issue = issue_factory(level=Issue.Level.WARNING)
    other = issue_factory(level=Issue.Level.ERROR)
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_changelist"), {"level": "warning"})
    body = response.content.decode()
    assert reverse("admin:admin_errors_issue_change", args=[warning_issue.pk]) in body
    assert reverse("admin:admin_errors_issue_change", args=[other.pk]) not in body


def test_filter_by_exception_type(client, django_user_model, issue_factory):
    a = issue_factory(exception_type="ValueError")
    other = issue_factory(exception_type="TypeError")
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(
        reverse("admin:admin_errors_issue_changelist"), {"exception_type": "ValueError"}
    )
    body = response.content.decode()
    assert reverse("admin:admin_errors_issue_change", args=[a.pk]) in body
    assert reverse("admin:admin_errors_issue_change", args=[other.pk]) not in body


def test_last_seen_filter_windows(client, django_user_model, issue_factory):
    now = timezone.now()
    recent = issue_factory(last_seen=now - datetime.timedelta(minutes=10))
    old = issue_factory(last_seen=now - datetime.timedelta(days=40))
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_changelist"), {"seen": "1h"})
    body = response.content.decode()
    assert reverse("admin:admin_errors_issue_change", args=[recent.pk]) in body
    assert reverse("admin:admin_errors_issue_change", args=[old.pk]) not in body


def test_search_matches_title(client, django_user_model, issue_factory):
    match = issue_factory(title="payment gateway timeout")
    other = issue_factory(title="unrelated failure", culprit="app.services.validate_order")
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_changelist"), {"q": "payment"})
    body = response.content.decode()
    assert reverse("admin:admin_errors_issue_change", args=[match.pk]) in body
    assert reverse("admin:admin_errors_issue_change", args=[other.pk]) not in body


def test_search_matches_exception_type(client, django_user_model, issue_factory):
    match = issue_factory(exception_type="ZeroDivisionError")
    other = issue_factory(exception_type="KeyError")
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(
        reverse("admin:admin_errors_issue_changelist"), {"q": "ZeroDivisionError"}
    )
    body = response.content.decode()
    assert reverse("admin:admin_errors_issue_change", args=[match.pk]) in body
    assert reverse("admin:admin_errors_issue_change", args=[other.pk]) not in body


def test_search_matches_culprit(client, django_user_model, issue_factory):
    match = issue_factory(culprit="app.services.validate_order")
    other = issue_factory(culprit="app.services.fetch_invoice")
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_changelist"), {"q": "validate_order"})
    body = response.content.decode()
    assert reverse("admin:admin_errors_issue_change", args=[match.pk]) in body
    assert reverse("admin:admin_errors_issue_change", args=[other.pk]) not in body


def test_default_ordering_is_last_seen_desc():
    ma = admin_module.IssueAdmin(Issue, admin_module.admin.site)
    assert ma.ordering == ["-last_seen"]


# -- T7: issue detail -------------------------------------------------------------------------


def test_detail_renders_traceback_frames_and_context_lines(
    client, django_user_model, issue_factory
):
    issue = issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue", "view_issue_context"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    body = response.content.decode()
    assert "process_payment" in body
    assert "raise RuntimeError(message)" in body


def test_detail_renders_chained_exception_separator(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue", "view_issue_context"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    assert "direct cause" in response.content.decode()


def test_detail_renders_request_section(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue", "view_issue_context"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    body = response.content.decode()
    assert "/sensitive/" in body
    assert SECRET_HEADER in body


def test_detail_renders_events_list_and_chart(client, django_user_model, issue_factory):
    issue = issue_factory(events=3)
    user = _staff_user(django_user_model, perms=["view_issue", "view_issue_context"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    body = response.content.decode()
    event_urls = [
        reverse("admin:admin_errors_issue_event", args=[issue.pk, event.pk])
        for event in issue.events.all()
    ]
    assert all(url in body for url in event_urls)
    assert "<svg" in body


def test_detail_renders_events_without_a_celery_key(
    client, django_user_model, issue_factory, payload_factory
):
    """Regression (found via e2e/test_admin_ui.py): a plain HTTP capture has no `celery` key at
    all (context.py only adds it when Celery info is present), unlike every other fixture in this
    module. `occurrences.html`'s Path/task column must not 500 on that common shape."""
    payload = payload_factory()
    del payload["celery"]
    issue = issue_factory(payload=payload, events=1)
    user = _staff_user(django_user_model, perms=["view_issue", "view_issue_context"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    assert response.status_code == 200
    assert payload["request"]["path"] in response.content.decode()


def test_detail_renders_celery_and_extra_without_a_request_key(
    client, django_user_model, issue_factory, payload_factory
):
    """Regression: a Celery task error or `capture_message()` from a script has `celery`/`extra`
    but no `request` key (context.build_payload only adds `request` when there is one). The
    Celery/Extra sections used to be nested inside `request.html`'s `{% if ae_payload.request %}`
    guard, so they never rendered for exactly the payloads they exist to serve."""
    payload = payload_factory()
    del payload["request"]
    issue = issue_factory(payload=payload, events=1)
    user = _staff_user(django_user_model, perms=["view_issue", "view_issue_context"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    assert response.status_code == 200
    body = response.content.decode()
    assert payload["celery"]["task"] in body
    assert payload["extra"]["request_id"] in body


def test_detail_hides_celery_and_extra_without_view_issue_context(
    client, django_user_model, issue_factory, payload_factory
):
    payload = payload_factory()
    del payload["request"]
    issue = issue_factory(payload=payload, events=1)
    user = _staff_user(django_user_model, perms=["view_issue"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    assert response.status_code == 200
    body = response.content.decode()
    assert payload["celery"]["task"] not in body
    assert payload["extra"]["request_id"] not in body


def _assert_no_nested_forms(body: str) -> None:
    """Nested `<form>` elements are invalid HTML: a browser silently reparents the inner one's
    controls into the outer form, so a submit button's real target becomes whatever the *outer*
    form's action is, not the inner one (found via e2e/test_admin_ui.py: the Resolve button's
    click ended up POSTing to the change URL instead of the status-transition URL)."""
    depth = 0
    for match in re.finditer(r"</?form\b", body):
        if match.group().startswith("</"):
            depth -= 1
        else:
            assert depth == 0, f"<form> nested inside another <form> at offset {match.start()}"
            depth += 1
    assert depth == 0, "unbalanced <form>/</form> tags"


def test_change_view_has_no_nested_status_forms(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    _assert_no_nested_forms(response.content.decode())


def test_event_detail_has_no_nested_status_forms(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    event = issue.events.first()
    response = client.get(reverse("admin:admin_errors_issue_event", args=[issue.pk, event.pk]))
    _assert_no_nested_forms(response.content.decode())


def test_detail_marks_in_app_and_library_frames(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue", "view_issue_context"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    body = response.content.decode()
    assert "ae-frame--in-app" in body
    assert "ae-frame--library" in body


def test_detail_frame_toggles_carry_aria_expanded(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue", "view_issue_context"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    body = response.content.decode()
    toggle_count = body.count("ae-frame-toggle")
    assert toggle_count > 0
    assert body.count("aria-expanded=") == toggle_count
    assert 'aria-expanded="true"' in body
    assert 'aria-expanded="false"' in body


def test_detail_renders_when_last_event_is_null(client, django_user_model, issue_factory):
    issue = issue_factory()
    issue.last_event = None
    issue.save(update_fields=["last_event"])
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    assert response.status_code == 200
    assert "No event data recorded yet." in response.content.decode()


def test_detail_renders_message_only_payload(
    client, django_user_model, issue_factory, payload_factory
):
    payload = payload_factory()
    del payload["exception"]
    del payload["frames"]
    issue = issue_factory(payload=payload)
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    assert response.status_code == 200
    assert payload["message"] in response.content.decode()


def test_detail_query_budget(
    client, django_user_model, issue_factory, django_assert_max_num_queries
):
    issue = issue_factory(events=5, days=30)
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    with django_assert_max_num_queries(15):
        response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    assert response.status_code == 200


# -- T8: event detail view --------------------------------------------------------------------


def test_event_detail_renders(client, django_user_model, issue_factory):
    issue = issue_factory(events=1)
    event = issue.events.get()
    user = _staff_user(django_user_model, perms=["view_issue", "view_issue_context"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_event", args=[issue.pk, event.pk]))
    assert response.status_code == 200
    assert SECRET_HEADER in response.content.decode()


def test_event_detail_404_for_an_event_of_another_issue(client, django_user_model, issue_factory):
    issue_a = issue_factory(events=1)
    issue_b = issue_factory(events=1)
    event_of_b = issue_b.events.get()
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(
        reverse("admin:admin_errors_issue_event", args=[issue_a.pk, event_of_b.pk])
    )
    assert response.status_code == 404


def test_event_detail_requires_view_permission(client, django_user_model, issue_factory):
    issue = issue_factory(events=1)
    event = issue.events.get()
    response = client.get(reverse("admin:admin_errors_issue_event", args=[issue.pk, event.pk]))
    assert response.status_code == 302
    assert reverse("admin:login") in response.url


# -- T9: status transitions --------------------------------------------------------------------


def test_single_resolve_sets_status_and_redirects(client, django_user_model, issue_factory):
    issue = issue_factory(status=Issue.Status.OPEN)
    user = _staff_user(django_user_model, perms=["view_issue", "change_issue"])
    client.force_login(user)
    response = client.post(reverse("admin:admin_errors_issue_status", args=[issue.pk, "resolve"]))
    assert response.status_code == 302
    issue.refresh_from_db()
    assert issue.status == Issue.Status.RESOLVED
    assert issue.resolved_at is not None
    assert issue.resolved_by == user


def test_single_ignore(client, django_user_model, issue_factory):
    issue = issue_factory(status=Issue.Status.OPEN)
    user = _staff_user(django_user_model, perms=["view_issue", "change_issue"])
    client.force_login(user)
    client.post(reverse("admin:admin_errors_issue_status", args=[issue.pk, "ignore"]))
    issue.refresh_from_db()
    assert issue.status == Issue.Status.IGNORED


def test_single_reopen_keeps_resolved_at(client, django_user_model, issue_factory):
    resolved_at = timezone.now() - datetime.timedelta(hours=2)
    issue = issue_factory(status=Issue.Status.RESOLVED, resolved_at=resolved_at)
    user = _staff_user(django_user_model, perms=["view_issue", "change_issue"])
    client.force_login(user)
    client.post(reverse("admin:admin_errors_issue_status", args=[issue.pk, "reopen"]))
    issue.refresh_from_db()
    assert issue.status == Issue.Status.OPEN
    assert issue.resolved_at == resolved_at


@pytest.mark.parametrize(
    "action, start_status, expected_status",
    [
        ("resolve", Issue.Status.OPEN, Issue.Status.RESOLVED),
        ("ignore", Issue.Status.OPEN, Issue.Status.IGNORED),
        ("reopen", Issue.Status.RESOLVED, Issue.Status.OPEN),
    ],
)
def test_bulk_actions_update_rows(
    client, django_user_model, issue_factory, action, start_status, expected_status
):
    resolved_at = timezone.now() - datetime.timedelta(hours=2) if action == "reopen" else None
    issues = [issue_factory(status=start_status, resolved_at=resolved_at) for _ in range(3)]
    user = _staff_user(django_user_model, perms=["view_issue", "change_issue"])
    client.force_login(user)
    data = {
        "action": action,
        "_selected_action": [str(i.pk) for i in issues],
    }
    response = client.post(reverse("admin:admin_errors_issue_changelist"), data, follow=True)
    assert response.status_code == 200
    for issue in issues:
        issue.refresh_from_db()
        assert issue.status == expected_status
        if action == "reopen":
            assert issue.resolved_at == resolved_at


def test_status_change_fires_issue_status_changed(client, django_user_model, issue_factory):
    received = []

    def _receiver(sender, **kwargs):
        received.append(kwargs)

    from admin_errors import signals

    signals.issue_status_changed.connect(_receiver)
    try:
        issue = issue_factory(status=Issue.Status.OPEN)
        user = _staff_user(django_user_model, perms=["view_issue", "change_issue"])
        client.force_login(user)
        client.post(reverse("admin:admin_errors_issue_status", args=[issue.pk, "resolve"]))
        assert len(received) == 1
        assert received[0]["old_status"] == Issue.Status.OPEN
        assert received[0]["new_status"] == Issue.Status.RESOLVED
        assert received[0]["user"] == user

        issues = [issue_factory(status=Issue.Status.OPEN) for _ in range(2)]
        received.clear()
        data = {"action": "ignore", "_selected_action": [str(i.pk) for i in issues]}
        client.post(reverse("admin:admin_errors_issue_changelist"), data)
        assert len(received) == 2
    finally:
        signals.issue_status_changed.disconnect(_receiver)


def test_status_view_rejects_get(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue", "change_issue"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_status", args=[issue.pk, "resolve"]))
    assert response.status_code == 405


def test_unknown_action_is_404(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue", "change_issue"])
    client.force_login(user)
    response = client.post(reverse("admin:admin_errors_issue_status", args=[issue.pk, "explode"]))
    assert response.status_code == 404


def test_repeated_resolve_is_idempotent(client, django_user_model, issue_factory):
    from admin_errors import signals

    received = []

    def _receiver(sender, **kwargs):
        received.append(kwargs)

    issue = issue_factory(status=Issue.Status.OPEN)
    user = _staff_user(django_user_model, perms=["view_issue", "change_issue"])
    client.force_login(user)
    client.post(reverse("admin:admin_errors_issue_status", args=[issue.pk, "resolve"]))

    signals.issue_status_changed.connect(_receiver)
    try:
        client.post(reverse("admin:admin_errors_issue_status", args=[issue.pk, "resolve"]))
        assert received == []
    finally:
        signals.issue_status_changed.disconnect(_receiver)

    issue.refresh_from_db()
    assert issue.status == Issue.Status.RESOLVED


# -- T10: permission gating -------------------------------------------------------------------

_ALL_SECRETS = [SECRET_HEADER, SECRET_COOKIE, SECRET_POST, SECRET_LOCAL, SECRET_USERNAME]


def test_view_issue_only_hides_context(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue"])
    client.force_login(user)
    detail = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    event = issue.events.first()
    event_detail = client.get(reverse("admin:admin_errors_issue_event", args=[issue.pk, event.pk]))
    for response in (detail, event_detail):
        assert response.status_code == 200
        body = response.content.decode()
        for secret in _ALL_SECRETS:
            assert secret not in body
        # paired positive assertion: the page still renders real content, not an empty shell
        assert issue.title in body


def test_view_issue_context_reveals_context(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue", "view_issue_context"])
    client.force_login(user)
    detail = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    event = issue.events.first()
    event_detail = client.get(reverse("admin:admin_errors_issue_event", args=[issue.pk, event.pk]))
    for response in (detail, event_detail):
        assert response.status_code == 200
        body = response.content.decode()
        for secret in _ALL_SECRETS:
            assert secret in body


def test_without_change_issue_buttons_are_absent_and_post_is_403(
    client, django_user_model, issue_factory
):
    issue = issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    body = response.content.decode()
    status_url = reverse("admin:admin_errors_issue_status", args=[issue.pk, "resolve"])
    assert status_url not in body
    post = client.post(reverse("admin:admin_errors_issue_status", args=[issue.pk, "resolve"]))
    assert post.status_code == 403


def test_without_delete_issue_delete_link_absent_and_post_is_403(
    client, django_user_model, issue_factory
):
    issue = issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue", "change_issue"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    body = response.content.decode()
    assert "ae-delete-link" not in body
    delete_response = client.post(
        reverse("admin:admin_errors_issue_delete", args=[issue.pk]), {"post": "yes"}
    )
    assert delete_response.status_code == 403


def test_superuser_sees_everything(client, django_user_model, issue_factory):
    issue = issue_factory()
    user = _staff_user(django_user_model, superuser=True)
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_change", args=[issue.pk]))
    body = response.content.decode()
    for secret in _ALL_SECRETS:
        assert secret in body
    assert "ae-delete-link" in body


def test_actions_absent_without_change_permission(client, django_user_model, issue_factory):
    issue_factory()
    user = _staff_user(django_user_model, perms=["view_issue"])
    client.force_login(user)
    response = client.get(reverse("admin:admin_errors_issue_changelist"))
    body = response.content.decode()
    assert 'value="resolve"' not in body
    assert 'value="ignore"' not in body
    assert 'value="reopen"' not in body


# -- T11: assets + budgets --------------------------------------------------------------------

_STATIC_ROOT = None


def _static_path(name):
    from pathlib import Path

    import admin_errors

    return Path(admin_errors.__file__).parent / "static" / "admin_errors" / name


def test_asset_budgets():
    css_bytes = _static_path("admin_errors.css").read_bytes()
    js_bytes = _static_path("admin_errors.js").read_bytes()
    assert len(css_bytes) <= 6144
    assert len(js_bytes) <= 3072


def test_css_uses_no_hard_coded_colours():
    css_text = _static_path("admin_errors.css").read_text()
    without_var_calls = re.sub(r"var\([^)]*\)", "", css_text)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", without_var_calls), without_var_calls
    assert "rgb(" not in without_var_calls


def test_no_inline_script_in_templates():
    from pathlib import Path

    import admin_errors

    templates_dir = Path(admin_errors.__file__).parent / "templates"
    for path in templates_dir.rglob("*.html"):
        text = path.read_text()
        for match in re.finditer(r"<script\b[^>]*>", text):
            assert "src=" in match.group(0), f"{path}: {match.group(0)}"
