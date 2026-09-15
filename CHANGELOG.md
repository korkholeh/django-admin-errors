# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project scaffold: `pyproject.toml` (hatchling, `src/` layout, zero runtime deps beyond Django),
  `tox.ini` matrix, `Makefile`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`.
- Importable, behaviour-free `admin_errors` package: `__version__`, `AdminErrorsConfig`.
- Minimal test host (`tests/settings.py`, `tests/package_settings.py`) and the scaffold test suite.
- Browser-free e2e harness (`e2e/`) that no-ops until the demo project exists.
- `admin_errors.conf.settings`: cached proxy over the `ADMIN_ERRORS` dict, all 39 spec-defined keys
  with their defaults, reset on Django's `setting_changed` signal.
- System checks `admin_errors.W001` (unknown setting key) and `admin_errors.E002` (SQLite build
  without JSON1 support), registered from `AppConfig.ready()`.
- Data model: `Issue`, `Event`, `IssueDailyCount` and the single shipped migration
  (`0001_initial`), with the `view_issue_context` permission.
- `admin_errors.fingerprint`: the frozen sha1 grouping algorithm for exceptions, message-only log
  records and explicit overrides, pinned by golden-value tests (ADR 0003).
