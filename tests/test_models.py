from pathlib import Path

import pytest
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from admin_errors.models import Event, Issue, IssueDailyCount

pytestmark = pytest.mark.django_db


def _make_issue(**kwargs):
    now = timezone.now()
    fields = {
        "fingerprint": "a" * 40,
        "exception_type": "ValueError",
        "title": "boom",
        "first_seen": now,
        "last_seen": now,
    }
    fields.update(kwargs)
    return Issue.objects.create(**fields)


def test_migrate_creates_the_issue_permissions():
    codenames = set(
        Permission.objects.filter(content_type__app_label="admin_errors").values_list(
            "codename", flat=True
        )
    )
    assert {"add_issue", "change_issue", "delete_issue", "view_issue", "view_issue_context"} <= (
        codenames
    )


def test_issue_meta_ordering_and_index():
    assert Issue._meta.ordering == ["-last_seen"]
    index_names = {index.name for index in Issue._meta.indexes}
    assert "ae_issue_status_seen_idx" in index_names
    matching = next(i for i in Issue._meta.indexes if i.name == "ae_issue_status_seen_idx")
    assert matching.fields == ["status", "-last_seen"]


def test_event_meta_index():
    index_names = {index.name for index in Event._meta.indexes}
    assert "ae_event_issue_ts_idx" in index_names
    matching = next(i for i in Event._meta.indexes if i.name == "ae_event_issue_ts_idx")
    assert matching.fields == ["issue", "-timestamp"]


def test_issue_daily_count_meta_constraint_and_index():
    constraint_names = {c.name for c in IssueDailyCount._meta.constraints}
    assert "ae_daily_issue_date_uniq" in constraint_names
    index_names = {index.name for index in IssueDailyCount._meta.indexes}
    assert "ae_daily_date_idx" in index_names


def test_duplicate_fingerprint_raises_integrity_error():
    _make_issue(fingerprint="b" * 40)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _make_issue(fingerprint="b" * 40)


def test_issue_daily_count_unique_constraint_enforced():
    issue = _make_issue(fingerprint="c" * 40)
    today = timezone.now().date()
    IssueDailyCount.objects.create(issue=issue, date=today, count=1)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            IssueDailyCount.objects.create(issue=issue, date=today, count=2)


def test_events_related_name_fetches_in_one_query(django_assert_num_queries):
    issue = _make_issue(fingerprint="d" * 40)
    Event.objects.create(issue=issue, timestamp=timezone.now(), payload={"v": 1})
    with django_assert_num_queries(1):
        list(issue.events.all())


def test_explicit_index_and_constraint_names_reach_the_schema():
    with connection.cursor() as cursor:
        issue_constraints = connection.introspection.get_constraints(cursor, Issue._meta.db_table)
        event_constraints = connection.introspection.get_constraints(cursor, Event._meta.db_table)
        daily_constraints = connection.introspection.get_constraints(
            cursor, IssueDailyCount._meta.db_table
        )
    assert "ae_issue_status_seen_idx" in issue_constraints
    assert "ae_event_issue_ts_idx" in event_constraints
    assert "ae_daily_date_idx" in daily_constraints
    assert "ae_daily_issue_date_uniq" in daily_constraints
    assert daily_constraints["ae_daily_issue_date_uniq"]["unique"] is True


def test_models_have_no_pending_migrations():
    call_command("makemigrations", "admin_errors", check=True, dry_run=True, verbosity=0)


def test_exactly_one_migration_ships():
    migrations_dir = Path(__file__).resolve().parent.parent / "src/admin_errors/migrations"
    modules = sorted(p.name for p in migrations_dir.glob("*.py") if p.name != "__init__.py")
    assert modules == ["0001_initial.py"]
