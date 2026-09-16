import sqlite3

import pytest
from django.core.checks import Error, Warning, run_checks
from django.test import override_settings

from admin_errors import checks
from admin_errors.conf import DEFAULTS, settings

# Literal expected table from spec section 5 — not re-derived from DEFAULTS, so a change to one
# without the other fails this test.
EXPECTED_DEFAULTS = {
    "ENABLED": True,
    "DATABASE": "default",
    "TRANSPORT": "thread",
    "CAPTURE_LEVEL": "ERROR",
    "AUTO_INSTALL_LOGGING_HANDLER": True,
    "CAPTURE_IN_DEBUG": True,
    "IGNORE_LOGGERS": ["django.security.DisallowedHost"],
    "IGNORE_EXCEPTIONS": [
        "django.http.Http404",
        "django.core.exceptions.PermissionDenied",
    ],
    "IGNORE_HTTP_STATUS_BELOW": 500,
    "BEFORE_SEND": None,
    "IN_APP_INCLUDE": None,
    "IN_APP_EXCLUDE": ["site-packages", "dist-packages", "/lib/python"],
    "CAPTURE_LOCALS": True,
    "CAPTURE_REQUEST_BODY": False,
    "MAX_BODY_BYTES": 4096,
    "MAX_VAR_REPR_LENGTH": 200,
    "MAX_FRAMES": 50,
    "MAX_PAYLOAD_BYTES": 65536,
    "EVENTS_PER_ISSUE": 20,
    "EVENT_SAMPLE_PER_HOUR": 10,
    "NEW_ISSUES_PER_MINUTE": 50,
    "QUEUE_MAXSIZE": 1000,
    "FLUSH_INTERVAL_SECONDS": 1.0,
    "FLUSH_BATCH_SIZE": 200,
    "EVENT_RETENTION_DAYS": 30,
    "DAILY_COUNT_RETENTION_DAYS": 90,
    "RESOLVED_ISSUE_TTL_DAYS": 14,
    "OPEN_ISSUE_TTL_DAYS": 90,
    "IGNORED_ISSUE_TTL_DAYS": None,
    "MAX_ISSUES": 5000,
    "CLEANUP": "opportunistic",
    "CLEANUP_INTERVAL_SECONDS": 3600,
    "SQLITE_VACUUM": "incremental",
    "NOTIFY_BACKEND": "admin_errors.notifications.EmailNotifier",
    "NOTIFY_RECIPIENTS": None,
    "NOTIFY_THROTTLE_SECONDS": 3600,
    "NOTIFY_ON": ["created", "regressed"],
    "NOTIFY_BASE_URL": "",
    "ADMIN_SITE": None,
    "INTERNAL_LOGGING": False,
}


def test_defaults_match_spec_section_5():
    assert DEFAULTS == EXPECTED_DEFAULTS


def test_default_test_host_enables_capture():
    # tests/settings.py overrides ADMIN_ERRORS = {"TRANSPORT": "sync"}; everything else is default.
    assert settings.ENABLED is True
    assert settings.TRANSPORT == "sync"
    assert settings.EVENTS_PER_ISSUE == 20


def test_override_settings_is_visible_and_reverts():
    assert settings.EVENTS_PER_ISSUE == 20
    with override_settings(ADMIN_ERRORS={"EVENTS_PER_ISSUE": 3}):
        assert settings.EVENTS_PER_ISSUE == 3
        assert settings.ENABLED is True  # other keys still fall back to their default
    assert settings.EVENTS_PER_ISSUE == 20


def test_notify_base_url_default():
    assert settings.NOTIFY_BASE_URL == ""
    with override_settings(ADMIN_ERRORS={"NOTIFY_BASE_URL": "https://errors.example.com"}):
        assert settings.NOTIFY_BASE_URL == "https://errors.example.com"
        assert [m for m in run_checks() if m.id == checks.W001_ID] == []


def test_unknown_attribute_raises_attribute_error():
    with pytest.raises(AttributeError):
        _ = settings.NOT_A_REAL_SETTING


def test_unknown_keys_is_empty_for_the_test_host():
    assert settings.unknown_keys() == []


def test_as_dict_is_a_merged_copy():
    merged = settings.as_dict()
    assert merged == {**DEFAULTS, "TRANSPORT": "sync"}
    assert merged is not settings._resolved()


def test_default_test_settings_produce_no_admin_errors_messages():
    messages = run_checks()
    assert [m for m in messages if m.id and m.id.startswith("admin_errors.")] == []


def test_unknown_setting_key_raises_w001():
    # Goes through run_checks(), not checks.check_settings_keys() directly, so the ready()
    # registration itself is load-bearing: deleting the register() call would also fail this.
    with override_settings(ADMIN_ERRORS={"NOPE": 1}):
        messages = [m for m in run_checks() if m.id == checks.W001_ID]
    assert len(messages) == 1
    assert isinstance(messages[0], Warning)
    assert "NOPE" in messages[0].msg


def test_old_sqlite_raises_e002(monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 8, 3))
    # Goes through run_checks(), not checks.check_sqlite_version() directly, for the same reason
    # as test_unknown_setting_key_raises_w001: the ready() registration must be load-bearing.
    messages = [m for m in run_checks() if m.id == checks.E002_ID]
    # tests/settings.py runs SQLite by default and Postgres under DJANGO_DB=postgres.
    from django.conf import settings as django_settings

    engine = django_settings.DATABASES[settings.DATABASE]["ENGINE"]
    if engine.endswith("sqlite3"):
        assert len(messages) == 1
        assert isinstance(messages[0], Error)
    else:
        assert messages == []


def test_e002_is_skipped_for_an_unknown_database_alias(monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 8, 3))
    with override_settings(ADMIN_ERRORS={"DATABASE": "nope"}):
        assert [m for m in run_checks() if m.id == checks.E002_ID] == []


def test_e001_for_missing_database_alias():
    with override_settings(ADMIN_ERRORS={"DATABASE": "nope"}):
        messages = [m for m in run_checks() if m.id == checks.E001_ID]
    assert len(messages) == 1
    assert isinstance(messages[0], Error)
    assert "nope" in messages[0].msg


def test_e001_silent_for_a_configured_alias():
    assert [m for m in run_checks() if m.id == checks.E001_ID] == []


def test_w002_propagate_false_without_admin_errors_handler():
    logging_config = {
        "version": 1,
        "loggers": {"django.request": {"handlers": ["console"], "propagate": False}},
    }
    with override_settings(LOGGING=logging_config):
        messages = [m for m in run_checks() if m.id == checks.W002_ID]
    assert len(messages) == 1
    assert isinstance(messages[0], Warning)
    assert "django.request" in messages[0].msg


def test_w002_silent_when_admin_errors_handler_present():
    logging_config = {
        "version": 1,
        "handlers": {"admin_errors": {"class": "admin_errors.handlers.AdminErrorsHandler"}},
        "loggers": {
            "django.request": {"handlers": ["admin_errors"], "propagate": False},
        },
    }
    with override_settings(LOGGING=logging_config):
        messages = [m for m in run_checks() if m.id == checks.W002_ID]
    assert messages == []


def test_w002_propagate_false_on_django_logger():
    logging_config = {
        "version": 1,
        "loggers": {"django": {"handlers": ["console"], "propagate": False}},
    }
    with override_settings(LOGGING=logging_config):
        messages = [m for m in run_checks() if m.id == checks.W002_ID]
    assert len(messages) == 1
    assert "django" in messages[0].msg


def test_w002_silent_with_no_logging_setting():
    assert [m for m in run_checks() if m.id == checks.W002_ID] == []


def test_w002_silent_when_admin_errors_handler_declared_via_factory_key():
    logging_config = {
        "version": 1,
        "handlers": {"admin_errors": {"()": "admin_errors.handlers.AdminErrorsHandler"}},
        "loggers": {
            "django.request": {"handlers": ["admin_errors"], "propagate": False},
        },
    }
    with override_settings(LOGGING=logging_config):
        messages = [m for m in run_checks() if m.id == checks.W002_ID]
    assert messages == []


def test_w003_mail_admins_overlap():
    logging_config = {
        "version": 1,
        "handlers": {"mail_admins": {"class": "django.utils.log.AdminEmailHandler"}},
        "root": {"handlers": ["mail_admins"], "level": "ERROR"},
    }
    with override_settings(LOGGING=logging_config):
        messages = [m for m in run_checks() if m.id == checks.W003_ID]
    assert len(messages) == 1
    assert isinstance(messages[0], Warning)


def test_w003_silent_when_notify_backend_none():
    logging_config = {
        "version": 1,
        "handlers": {"mail_admins": {"class": "django.utils.log.AdminEmailHandler"}},
        "root": {"handlers": ["mail_admins"], "level": "ERROR"},
    }
    with override_settings(LOGGING=logging_config, ADMIN_ERRORS={"NOTIFY_BACKEND": None}):
        messages = [m for m in run_checks() if m.id == checks.W003_ID]
    assert messages == []


def test_w003_silent_when_handler_unreferenced():
    logging_config = {
        "version": 1,
        "handlers": {"mail_admins": {"class": "django.utils.log.AdminEmailHandler"}},
    }
    with override_settings(LOGGING=logging_config):
        messages = [m for m in run_checks() if m.id == checks.W003_ID]
    assert messages == []


def test_w003_silent_with_no_logging_setting():
    assert [m for m in run_checks() if m.id == checks.W003_ID] == []


def test_w003_tolerates_malformed_logging_shapes():
    """review r1 nit: a host mid-edit of `LOGGING` may have handlers/loggers/root as the wrong
    type; `check_mail_admins_overlap` must report [] rather than raise. Calls the check function
    directly (not `run_checks()`) since some of these malformed shapes also break the unrelated,
    pre-existing `check_logging_propagation` (W002), which is out of scope for this fix."""
    for logging_config in (
        {"version": 1, "handlers": ["not", "a", "dict"]},
        {
            "version": 1,
            "handlers": {"mail_admins": {"class": "django.utils.log.AdminEmailHandler"}},
            "root": "not-a-dict",
        },
        {
            "version": 1,
            "handlers": {"mail_admins": {"class": "django.utils.log.AdminEmailHandler"}},
            "loggers": {"django.request": "not-a-dict"},
        },
        {
            "version": 1,
            "handlers": {"mail_admins": {"class": "django.utils.log.AdminEmailHandler"}},
            "root": {"handlers": "not-a-list"},
        },
        "not-a-dict-at-all",
    ):
        with override_settings(LOGGING=logging_config):
            assert checks.check_mail_admins_overlap() == []


def test_w003_triggers_via_a_named_logger_not_only_root():
    logging_config = {
        "version": 1,
        "handlers": {"mail_admins": {"class": "django.utils.log.AdminEmailHandler"}},
        "loggers": {"django.request": {"handlers": ["mail_admins"], "level": "ERROR"}},
    }
    with override_settings(LOGGING=logging_config):
        messages = [m for m in run_checks() if m.id == checks.W003_ID]
    assert len(messages) == 1
