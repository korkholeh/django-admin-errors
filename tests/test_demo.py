"""Drives every demo URL (spec section 13) and `demo_seed` through the main test gate.

`uv run pytest -q` is one session bound to `DJANGO_SETTINGS_MODULE=tests.settings`, so the demo
project cannot get its own settings module here. Instead this module imports
`demo_project.settings` as data and reapplies the parts that matter through `override_settings`,
which keeps the test honest against the real demo configuration instead of re-declaring it (see
DECISIONS.md p07-plan). `pyproject.toml`'s `pythonpath` includes `demo/`, which is what makes
`import demo_project` / `import demo_app` work here.
"""

from __future__ import annotations

import logging
import time

import demo_project.settings as demo_settings
import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import authenticate
from django.core.management import call_command
from django.test import AsyncClient, Client, override_settings
from django.utils import timezone

from admin_errors import api
from admin_errors.apps import AdminErrorsConfig
from admin_errors.handlers import AdminErrorsHandler
from admin_errors.models import Event, Issue, IssueDailyCount
from tests.settings import INSTALLED_APPS as HOST_INSTALLED_APPS

pytestmark = pytest.mark.django_db

DEMO_INSTALLED_APPS = [*HOST_INSTALLED_APPS, "demo_app"]
# TRANSPORT forced to "sync" (CLAUDE.md convention, risk #6): every assertion below runs against a
# settled database with no writer thread. The one case that must prove the thread transport for
# real (the 5000-storm timing criterion) opts back into "thread" explicitly.
DEMO_ADMIN_ERRORS_SYNC = {**demo_settings.ADMIN_ERRORS, "TRANSPORT": "sync"}


@pytest.fixture(autouse=True)
def demo_urls():
    # `override_settings(INSTALLED_APPS=...)` calls `apps.set_installed_apps()` *before* any of
    # the overridden settings (including `ADMIN_ERRORS`) are actually applied (see
    # `django.test.utils.override_settings.enable`), so the `AppConfig.ready()` rerun that
    # triggers still installs `AdminErrorsHandler` at the host's original `CAPTURE_LEVEL`
    # ("ERROR"), not the demo's ("WARNING"). Removing and reinstalling it *inside* the
    # `with override_settings(...)` block, once every override is actually live, is what makes
    # `CAPTURE_LEVEL="WARNING"` real for this fixture's lifetime — the same pattern
    # `test_capture.py::test_capture_level_below_warning_lowers_the_root_logger` uses for a single
    # setting, extended to survive the `INSTALLED_APPS` side effect.
    root = logging.getLogger()
    original_level = root.level
    installed = [handler for handler in root.handlers if isinstance(handler, AdminErrorsHandler)]
    with override_settings(
        ROOT_URLCONF="demo_project.urls",
        MIDDLEWARE=demo_settings.MIDDLEWARE,
        INSTALLED_APPS=DEMO_INSTALLED_APPS,
        ADMIN_ERRORS=DEMO_ADMIN_ERRORS_SYNC,
    ):
        for handler in list(root.handlers):
            if isinstance(handler, AdminErrorsHandler):
                root.removeHandler(handler)
        AdminErrorsConfig._install_logging_handler()
        try:
            yield
        finally:
            for handler in list(root.handlers):
                if isinstance(handler, AdminErrorsHandler):
                    root.removeHandler(handler)
            for handler in installed:
                root.addHandler(handler)
            root.setLevel(original_level)


def _client() -> Client:
    return Client(raise_request_exception=False)


# --- T7: URL sweep --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, expected_status",
    [
        ("/", 200),
        ("/boom/", 500),
        ("/boom/7/", 500),
        ("/keyerror/x/", 500),
        ("/nested/", 500),
        ("/logged/", 200),
        ("/warning/", 200),
        ("/sensitive/", 200),
        ("/storm/?n=3", 200),
        ("/unique-storm/?n=3", 200),
        ("/task/", 200),
        ("/404/", 404),
    ],
)
def test_every_demo_url_returns_its_expected_status(path, expected_status):
    response = _client().get(path)
    assert response.status_code == expected_status


def test_sensitive_post_returns_500():
    response = _client().post("/sensitive/", {"username": "demo", "password": "whatever"})
    assert response.status_code == 500


def test_boom_n_collapses_to_one_issue():
    client = _client()
    client.get("/boom/1/")
    client.get("/boom/2/")

    assert Issue.objects.count() == 1
    assert Issue.objects.get().count == 2


def test_keyerror_collapses_to_one_issue():
    client = _client()
    client.get("/keyerror/alpha/")
    client.get("/keyerror/bravo/")

    assert Issue.objects.count() == 1
    assert Issue.objects.get().count == 2


def test_http404_produces_no_issue():
    _client().get("/404/")
    assert Issue.objects.count() == 0


def test_logged_returns_200_and_captures_one_issue():
    response = _client().get("/logged/")
    assert response.status_code == 200
    assert Issue.objects.count() == 1


def test_warning_is_captured_at_warning_level():
    _client().get("/warning/")
    issue = Issue.objects.get()
    assert issue.level == Issue.Level.WARNING


def test_nested_stores_exception_chain():
    _client().get("/nested/")
    issue = Issue.objects.get()
    event = Event.objects.get(issue=issue)
    assert event.payload["exception"]["chain"]


def test_sensitive_view_payload_contains_no_secrets():
    from demo_app.views import DEMO_PASSWORD, DEMO_TOKEN

    _client().post("/sensitive/", {"username": "demo", "password": DEMO_PASSWORD})

    issue = Issue.objects.get()
    event = Event.objects.get(issue=issue)
    payload_text = str(event.payload)
    assert DEMO_PASSWORD not in payload_text
    assert DEMO_TOKEN not in payload_text
    assert f"Bearer {DEMO_TOKEN}" not in payload_text


@pytest.mark.django_db(transaction=True)
def test_async_boom_is_captured():
    client = AsyncClient(raise_request_exception=False)

    async def _do_request():
        return await client.get("/async-boom/")

    response = async_to_sync(_do_request)()

    assert response.status_code == 500
    assert Issue.objects.count() == 1


def test_task_view_produces_one_issue():
    response = _client().get("/task/")
    assert response.status_code == 200
    assert Issue.objects.count() == 1


# --- T8: storm and limiters ------------------------------------------------------------------


def test_storm_aggregates_into_one_issue():
    response = _client().get("/storm/?n=25")
    assert response.status_code == 200
    body = response.json()
    assert body["n"] == 25

    assert Issue.objects.count() == 1
    issue = Issue.objects.get()
    assert issue.count == 25
    assert (
        Event.objects.filter(issue=issue).count()
        == demo_settings.ADMIN_ERRORS["EVENT_SAMPLE_PER_HOUR"]
    )
    assert IssueDailyCount.objects.filter(issue=issue).count() == 1


def test_unique_storm_is_bounded_by_new_issue_admission():
    # NEW_ISSUES_PER_MINUTE is not overridden by the demo profile, so it stays on the shipped
    # default (50): 80 unique messages admitted well within the bucket's 60s refill window must
    # yield exactly 50 issues, the rest counted as dropped.
    response = _client().get("/unique-storm/?n=80")
    assert response.status_code == 200
    assert Issue.objects.count() == 50


@pytest.mark.django_db(transaction=True)
def test_storm_of_5000_returns_under_one_second():
    issues_before = Issue.objects.count()
    events_before = Event.objects.count()
    daily_before = IssueDailyCount.objects.count()

    with override_settings(ADMIN_ERRORS={**DEMO_ADMIN_ERRORS_SYNC, "TRANSPORT": "thread"}):
        start = time.monotonic()
        response = _client().get("/storm/?n=5000")
        wall_clock = time.monotonic() - start
        api.flush()

    assert response.status_code == 200
    body = response.json()
    assert body["n"] == 5000
    assert body["elapsed_seconds"] < 1.0
    assert wall_clock < 1.0

    # A tight single-threaded burst against the writer's bounded queue (`QUEUE_MAXSIZE`) can drop
    # some occurrences under contention (`writer.stats.dropped_queue_full`); the bound proved here
    # is the one PLAN.md's own acceptance table asks for: at most one issue, one daily count row,
    # and the sampling ceiling on events - not that every occurrence survived to `issue.count`.
    assert Issue.objects.count() - issues_before <= 1
    issue = Issue.objects.get(fingerprint=body["fingerprint"])
    assert 0 < issue.count <= 5000
    assert (
        Event.objects.count() - events_before <= demo_settings.ADMIN_ERRORS["EVENT_SAMPLE_PER_HOUR"]
    )
    assert IssueDailyCount.objects.count() - daily_before == 1


# --- T9: demo_seed -----------------------------------------------------------------------------


def test_demo_seed_creates_issues_and_daily_counts():
    call_command("demo_seed", "--issues", "40", "--days", "30")

    assert Issue.objects.count() == 40

    today = timezone.now().date()
    dates = set(IssueDailyCount.objects.values_list("date", flat=True))
    assert len(dates) >= 20
    for date in dates:
        assert date <= today
        assert (today - date).days < 30


def test_demo_seed_creates_superuser_admin():
    call_command("demo_seed", "--issues", "5", "--days", "10")

    user = authenticate(username="admin", password="admin")
    assert user is not None
    assert user.is_superuser


def test_demo_seed_reset_is_idempotent():
    def _snapshot():
        return sorted(
            (
                issue.fingerprint,
                issue.count,
                issue.status,
                tuple(
                    sorted(
                        IssueDailyCount.objects.filter(issue=issue).values_list("count", flat=True)
                    )
                ),
            )
            for issue in Issue.objects.all()
        )

    call_command("demo_seed", "--issues", "40", "--days", "30", "--reset")
    first = _snapshot()
    call_command("demo_seed", "--issues", "40", "--days", "30", "--reset")
    second = _snapshot()

    assert first == second


def test_demo_seed_is_not_capped_by_new_issue_admission():
    call_command("demo_seed", "--issues", "60", "--days", "30")
    assert Issue.objects.count() == 60


def test_demo_seed_creates_a_view_only_viewer():
    from django.contrib.auth import get_user_model

    call_command("demo_seed", "--issues", "5", "--days", "10", "--reset")
    call_command("demo_seed", "--issues", "5", "--days", "10", "--reset")

    user_model = get_user_model()
    assert user_model.objects.filter(username="viewer").count() == 1
    user = user_model.objects.get(username="viewer")
    assert user.is_staff
    assert not user.is_superuser
    codenames = set(user.user_permissions.values_list("codename", flat=True))
    assert codenames == {"view_issue"}
