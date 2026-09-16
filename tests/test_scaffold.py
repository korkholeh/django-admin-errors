import os
import subprocess
import sys

import pytest
from django.apps import apps
from django.conf import settings

import admin_errors
from tests.settings import database_from_url


def test_version() -> None:
    assert admin_errors.__version__ == "0.1.0"


def test_app_config() -> None:
    config = apps.get_app_config("admin_errors")
    assert config.verbose_name == "Errors"
    assert config.default_auto_field == "django.db.models.BigAutoField"


def test_import_has_no_side_effects(repo_root) -> None:
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", "import admin_errors; print(admin_errors.__version__)"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == admin_errors.__version__


def test_minimal_host_installed_apps() -> None:
    assert settings.INSTALLED_APPS == [
        "django.contrib.contenttypes",
        "django.contrib.auth",
        "django.contrib.sessions",
        "django.contrib.messages",
        "django.contrib.admin",
        "admin_errors",
    ]


def test_database_from_url_parses_postgres_dsn() -> None:
    parsed = database_from_url("postgres://u:p%40ss@h:5433/db")
    assert parsed == {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "db",
        "USER": "u",
        "PASSWORD": "p@ss",
        "HOST": "h",
        "PORT": "5433",
    }


def test_database_from_url_unquotes_username_and_database_name() -> None:
    parsed = database_from_url("postgres://u%40tenant:p%40ss@h:5433/db%20name")
    assert parsed["USER"] == "u@tenant"
    assert parsed["NAME"] == "db name"


def test_database_from_url_rejects_non_postgres_scheme() -> None:
    with pytest.raises(ValueError, match="scheme"):
        database_from_url("mysql://u:p@h:3306/db")


def test_database_from_url_rejects_scheme_less_string() -> None:
    with pytest.raises(ValueError, match="scheme"):
        database_from_url("not-a-url")
