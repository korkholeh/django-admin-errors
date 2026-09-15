import datetime
import tempfile
import uuid
import warnings
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.db import connection, connections
from django.db.backends.base.base import BaseDatabaseWrapper
from django.test import override_settings
from django.utils import timezone

# Fixed values a `view_issue`-only user must never see, so `tests/test_admin.py` can assert their
# *absence* from a rendered response instead of asserting on shape alone (spec 12.5, risk #2).
SECRET_HEADER = "secret-header-value-9f3a"
SECRET_COOKIE = "secret-cookie-value-7c1b"
SECRET_POST = "secret-post-value-4e2d"
SECRET_LOCAL = "secret-local-value-6b8f"
SECRET_USERNAME = "secret-username-5a9c"


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def postgres_only() -> None:
    """Skip unless the default alias is PostgreSQL (`DJANGO_DB=postgres`).

    The vendor is only knowable after Django is configured, which happens after collection
    imports the test module, so a module-level `skipif` cannot express this.
    """
    if connection.vendor != "postgresql":
        pytest.skip("only meaningful under DJANGO_DB=postgres")


def _allow_alias_in_active_test_case(alias: str) -> None:
    """Let the running `django_db`-marked test's TestCase touch `alias`.

    Django's `TestCase`/`TransactionTestCase` fix the set of allowed database aliases in
    `setUpClass`, before any fixture that runs inside the test body (like `sqlite_alias`'s factory
    call) can register a new one — `override_settings`/`databases="__all__"` are both resolved too
    early to help. The allowed set lives as a class attribute closed over by the patched
    `BaseDatabaseWrapper.ensure_connection` installed for the test; reaching into that closure to
    extend it in place is the only way a dynamically created alias can be used within the test.
    """
    ensure_connection = BaseDatabaseWrapper.ensure_connection
    freevars = getattr(ensure_connection.__code__, "co_freevars", ())
    if "cls" not in freevars or ensure_connection.__closure__ is None:
        return  # not running inside a django_db-marked TestCase; nothing to extend
    cell = ensure_connection.__closure__[freevars.index("cls")]
    test_case_cls = cell.cell_contents
    test_case_cls.databases = frozenset(test_case_cls.databases) | {alias}


def _invalidate_connections_settings() -> None:
    """Force `django.db.connections` to re-read `settings.DATABASES`.

    `ConnectionHandler.settings` is a `cached_property`, and its getter also freezes the resolved
    dict onto `self._settings` on first access — by the time any fixture runs, both are already
    populated, so `override_settings(DATABASES=…)` alone is invisible to it. Clearing both is what
    makes a newly registered alias reachable via `connections[alias]`.
    """
    connections._settings = None
    connections.__dict__.pop("settings", None)


@pytest.fixture
def sqlite_alias(request):
    """Factory for a throwaway file-backed SQLite alias, migrated and registered.

    `PRAGMA auto_vacuum` can only be set before any table exists in the file, and `VACUUM` needs a
    real connection outside of pytest-django's wrapping test transaction — both need a database
    that isn't the one `TestCase`/`django_db` already manages.

    Usage: `alias = sqlite_alias()` or `sqlite_alias(auto_vacuum=2)`. Each call registers and
    migrates a fresh alias and returns its name; the connection is closed and the file removed when
    the test ends. `database_routers`, when given, is applied in the *same* `override_settings`
    call as `DATABASES`: Django's override stack is a strict LIFO chain, and this fixture's
    finalizer always runs last (registered at fixture setup, before the test body), so any router
    override entered separately by the test and disabled *inside* the test body would be popped
    while this fixture's `DATABASES` override is still layered on top of it, corrupting the chain
    (see DECISIONS.md p05-implement/tests).
    """
    from django.conf import settings as django_settings

    created: list[tuple[Any, str, Path]] = []

    def _make(*, auto_vacuum: int | None = None, database_routers: list[str] | None = None) -> str:
        alias = f"scratch_{uuid.uuid4().hex}"
        path = Path(tempfile.gettempdir()) / f"admin_errors_{alias}.sqlite3"
        merged = {
            **django_settings.DATABASES,
            alias: {"ENGINE": "django.db.backends.sqlite3", "NAME": str(path)},
        }
        overrides: dict[str, Any] = {"DATABASES": merged}
        if database_routers is not None:
            overrides["DATABASE_ROUTERS"] = database_routers
        ctx = override_settings(**overrides)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            ctx.enable()
        _invalidate_connections_settings()
        _allow_alias_in_active_test_case(alias)
        created.append((ctx, alias, path))
        if auto_vacuum is not None:
            with connections[alias].cursor() as cursor:
                cursor.execute(f"PRAGMA auto_vacuum={auto_vacuum}")
        call_command("migrate", database=alias, verbosity=0, run_syncdb=True)
        return alias

    def _teardown() -> None:
        for ctx, alias, path in reversed(created):
            connections[alias].close()
            ctx.disable()
            _invalidate_connections_settings()
            path.unlink(missing_ok=True)

    request.addfinalizer(_teardown)
    return _make


@pytest.fixture(autouse=True)
def _reset_admin_errors_capture_state():
    """Reset process-local capture state so tests don't leak into each other.

    The recursion guard is thread-local and the internal-logging dedup set, the admission/sampling
    buckets and the writer thread/stats are process-global; none of these are reset by Django's
    `override_settings`/test isolation.
    """
    from admin_errors import capture, writer

    capture._recursion_guard.active = False
    capture._internal_logged_types.clear()
    capture.reset_rate_limits()
    writer.reset_for_tests()
    yield
    capture._recursion_guard.active = False
    capture._internal_logged_types.clear()
    capture.reset_rate_limits()
    writer.reset_for_tests()


@pytest.fixture
def payload_factory():
    """Factory for a full spec section 6.4 payload: chained exception, request, celery, extra.

    Every value that must stay hidden from a `view_issue`-only user is one of the module-level
    `SECRET_*` constants, so a permission test can `assertNotContains`/`assertContains` them by
    identity instead of guessing which rendered string would leak.
    """

    def _make(**overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "v": 1,
            "timestamp": "2026-09-15T12:00:00Z",
            "level": "error",
            "logger": "tests",
            "message": "process payment failed",
            "source": "exception",
            "exception": {
                "type": "RuntimeError",
                "module": "builtins",
                "value": "process payment failed",
                "chain": [
                    {
                        "type": "RuntimeError",
                        "module": "builtins",
                        "value": "process payment failed",
                        "cause": True,
                        "frames": [
                            {
                                "filename": "/app/views.py",
                                "lineno": 42,
                                "function": "process_payment",
                                "module": "app.views",
                                "in_app": True,
                                "pre_context": ["def process_payment(request):", "    try:"],
                                "context_line": "        raise RuntimeError(message)",
                                "post_context": ["    except ValueError as exc:", "        pass"],
                                "vars": {"token": SECRET_LOCAL, "amount": "100"},
                            },
                            {
                                "filename": (
                                    "/usr/lib/python3.11/site-packages/django/core/handlers.py"
                                ),
                                "lineno": 10,
                                "function": "_get_response",
                                "module": "django.core.handlers.base",
                                "in_app": False,
                                "pre_context": ["def _get_response(self, request):"],
                                "context_line": "    response = wrapped_callback(request)",
                                "post_context": ["    return response"],
                                "vars": {"self": "<Handler>"},
                            },
                        ],
                    },
                    {
                        "type": "ValueError",
                        "module": "builtins",
                        "value": "inner failure",
                        "cause": False,
                        "frames": [
                            {
                                "filename": "/app/services.py",
                                "lineno": 17,
                                "function": "validate_order",
                                "module": "app.services",
                                "in_app": True,
                                "pre_context": ["def validate_order(order):"],
                                "context_line": "    raise ValueError('inner failure')",
                                "post_context": ["    return True"],
                                "vars": {},
                            },
                        ],
                    },
                ],
            },
            "request": {
                "method": "POST",
                "path": "/sensitive/",
                "url": "http://testserver/sensitive/",
                "query": {"debug": "1"},
                "post": {"token": SECRET_POST},
                "headers": {"X-Api-Key": SECRET_HEADER, "User-Agent": "pytest"},
                "cookies": {"sessionid": SECRET_COOKIE},
                "remote_addr": "127.0.0.1",
                "user": {"id": 1, "username": SECRET_USERNAME, "email": "user@example.com"},
                "body": None,
            },
            "celery": {"task": "app.tasks.charge", "id": "abc-123", "args": "()", "kwargs": "{}"},
            "extra": {"request_id": "req-42"},
            "server": {
                "hostname": "demo-host",
                "pid": 4242,
                "python": "3.11.0",
                "django": "5.2.0",
            },
        }
        payload["frames"] = payload["exception"]["chain"][0]["frames"]
        payload.update(overrides)
        return payload

    return _make


@pytest.fixture
def issue_factory(payload_factory):
    """Factory for an `Issue` with events and 14 days of `IssueDailyCount` rows.

    `days` controls both `first_seen` (days ago) and how many daily-count rows are created, so
    tests exercising the sparkline/chart get real prefetchable data. Pass `payload=` to reuse a
    payload built (and customised) via `payload_factory` for a specific event.
    """

    def _make(
        *,
        events: int = 3,
        days: int = 14,
        status: str = "open",
        resolved_at: datetime.datetime | None = None,
        payload: dict[str, Any] | None = None,
        **issue_overrides: Any,
    ):
        from admin_errors.models import Event, Issue, IssueDailyCount

        now = timezone.now()
        first_seen = now - datetime.timedelta(days=days)
        fields: dict[str, Any] = {
            "fingerprint": uuid.uuid4().hex,
            "exception_type": "RuntimeError",
            "title": "process payment failed",
            "culprit": "app.views.process_payment",
            "level": Issue.Level.ERROR,
            "status": status,
            "first_seen": first_seen,
            "last_seen": now,
            "count": events,
            "resolved_at": resolved_at,
        }
        fields.update(issue_overrides)
        issue = Issue.objects.create(**fields)

        used_payload = payload or payload_factory()
        created_events = []
        for i in range(events):
            event = Event.objects.create(
                issue=issue, timestamp=now - datetime.timedelta(hours=i), payload=used_payload
            )
            created_events.append(event)
        if created_events:
            issue.last_event = used_payload
            issue.save(update_fields=["last_event"])

        date = first_seen.date()
        end = now.date()
        while date <= end:
            IssueDailyCount.objects.create(issue=issue, date=date, count=2)
            date += datetime.timedelta(days=1)

        return issue

    return _make
