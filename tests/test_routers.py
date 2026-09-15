import pytest
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import override_settings

from admin_errors import api
from admin_errors.models import Event, Issue, IssueDailyCount
from admin_errors.routers import AdminErrorsRouter

pytestmark = pytest.mark.django_db


def test_db_for_read_and_write_return_alias_for_admin_errors_models():
    router = AdminErrorsRouter()
    with override_settings(ADMIN_ERRORS={"DATABASE": "errors"}):
        for model in (Issue, Event, IssueDailyCount):
            assert router.db_for_read(model) == "errors"
            assert router.db_for_write(model) == "errors"


def test_db_for_read_and_write_return_none_for_foreign_models():
    router = AdminErrorsRouter()
    User = get_user_model()
    assert router.db_for_read(User) is None
    assert router.db_for_write(User) is None


def test_db_for_read_returns_none_for_user_even_with_issue_instance_hint():
    # The router must not claim to know where the host's user model lives, even when hinted by
    # one of our own instances (see DECISIONS.md p05-plan/routers).
    router = AdminErrorsRouter()
    User = get_user_model()
    issue = Issue(fingerprint="x" * 40)
    with override_settings(ADMIN_ERRORS={"DATABASE": "errors"}):
        assert router.db_for_read(User, instance=issue) is None
        assert router.db_for_write(User, instance=issue) is None


def test_allow_relation_true_when_either_side_is_admin_errors():
    router = AdminErrorsRouter()
    User = get_user_model()
    user = User(username="u")
    issue = Issue(fingerprint="x" * 40)
    assert router.allow_relation(issue, user) is True
    assert router.allow_relation(user, issue) is True


def test_allow_relation_none_for_two_foreign_models():
    router = AdminErrorsRouter()
    User = get_user_model()
    assert router.allow_relation(User(), User()) is None


def test_allow_migrate_true_on_configured_alias_false_elsewhere():
    router = AdminErrorsRouter()
    with override_settings(ADMIN_ERRORS={"DATABASE": "errors"}):
        assert router.allow_migrate("errors", "admin_errors") is True
        assert router.allow_migrate("default", "admin_errors") is False


def test_allow_migrate_none_for_other_apps():
    router = AdminErrorsRouter()
    assert router.allow_migrate("default", "auth") is None
    assert router.allow_migrate("errors", "auth") is None


@pytest.mark.django_db(databases=["default", "errors"])
def test_capture_writes_only_to_the_dedicated_alias():
    with override_settings(
        ADMIN_ERRORS={"TRANSPORT": "sync", "DATABASE": "errors"},
        DATABASE_ROUTERS=["admin_errors.routers.AdminErrorsRouter"],
    ):
        fp = api.capture_exception(ValueError("boom"))
    assert fp is not None
    assert Issue.objects.using("errors").count() == 1
    assert Event.objects.using("errors").count() == 1
    assert IssueDailyCount.objects.using("errors").count() == 1
    assert Issue.objects.using("default").count() == 0
    assert Event.objects.using("default").count() == 0
    assert IssueDailyCount.objects.using("default").count() == 0


def test_migrate_with_router_creates_no_admin_errors_tables(sqlite_alias):
    # conf.DATABASE defaults to "default", so the router refuses admin_errors migrations on the
    # freshly created scratch alias.
    alias = sqlite_alias(database_routers=["admin_errors.routers.AdminErrorsRouter"])
    tables = connections[alias].introspection.table_names()
    assert not [name for name in tables if name.startswith("admin_errors_")]


def test_migrate_without_router_creates_admin_errors_tables(sqlite_alias):
    alias = sqlite_alias()
    tables = connections[alias].introspection.table_names()
    assert "admin_errors_issue" in tables
