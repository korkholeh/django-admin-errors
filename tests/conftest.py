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
