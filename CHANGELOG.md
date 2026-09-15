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
- Synchronous capture pipeline (spec section 7.2): `admin_errors.context` builds the scrubbed
  event payload (frames, request block, chained exceptions) entirely through Django's own
  `get_exception_reporter_filter`; `admin_errors.capture` is the single chokepoint (guards,
  fingerprinting, `BEFORE_SEND`, `MAX_PAYLOAD_BYTES` degradation) and never raises or recurses;
  `admin_errors.storage.store_batch` writes issues, events and daily counts with PostgreSQL-safe
  savepointed creates. POST fields are cleansed by key (not only by `@sensitive_post_parameters`)
  and `Authorization`/`Proxy-Authorization`-style headers are redacted the same way on every
  supported Django version, including 4.2.
- Public API `admin_errors.api.capture_exception`/`capture_message`/`flush` (ADR 0008),
  `admin_errors.handlers.AdminErrorsHandler` (installed on the root logger from `ready()` when
  `AUTO_INSTALL_LOGGING_HANDLER`), `admin_errors.middleware.RequestContextMiddleware` (optional,
  sync/async capable) and the `got_request_exception` marker receiver. When `CAPTURE_LEVEL` is set
  below the host's root logger level (e.g. `"INFO"` or `"DEBUG"`), `ready()` lowers the root
  logger itself so those records actually reach the handler; this also makes them propagate to
  any other handler already attached to the root logger (console, file, aggregator). Hosts that
  want to keep their own handlers quiet at the default level should raise the level on those
  handlers instead of relying on the root logger's level.
- Background writer (spec section 9.1): `admin_errors.writer.Writer` runs all `TRANSPORT="thread"`
  database I/O in a lazily started daemon thread (never from `AppConfig.ready()`), aggregating
  queued items by fingerprint and flushing on batch size, on the flush interval, or on an explicit
  `flush()`. Own-connection hygiene: `close_old_connections()` before every flush, one retry after
  an `OperationalError` before the batch is dropped and counted, and the connection is closed after
  `IDLE_CONNECTION_SECONDS` of inactivity. A fork is detected on every `enqueue()` (`os.getpid()`)
  and restarts the queue and thread instead of reusing a stuck inherited one. `admin_errors.api.
  flush()` now really blocks until the writer's queue is drained (a no-op under `TRANSPORT="sync"`
  and when no writer thread was ever started).
- Admission and sampling (spec section 7.2 steps 4-5) bound a storm to one issue and a handful of
  events: a token bucket gates new fingerprints (`NEW_ISSUES_PER_MINUTE`, dropped occurrences are
  still counted, never silently lost) and a second, per-fingerprint bucket gates full-payload
  sampling (`EVENT_SAMPLE_PER_HOUR`) while every occurrence still counts towards the issue, sampled
  or not.
- `admin_errors.signals`: `issue_created`, `issue_regressed` and `issue_status_changed` (declared
  now, first fired from the admin in a later phase), plus `send_safely()` over Django's own
  `Signal.send_robust`. `storage.store_batch` fires `issue_created`/`issue_regressed` from
  `transaction.on_commit`, gated on `signal.has_listeners()` so the existing `assertNumQueries`
  bounds are unaffected while nothing is connected.
- `benchmarks/bench_capture.py`: a standalone timing harness (not collected by pytest) with a
  budget-gated exit code. Measured on this machine (2026-09-15, Python 3.13.3, SQLite,
  `TRANSPORT="thread"` with a no-op sink): capture with a 30-frame traceback and no locals,
  p50 ≈ 1.1 ms (budget ≤ 2 ms); with locals, p50 ≈ 1.3 ms (budget ≤ 10 ms); count-only (sampling
  budget exhausted), p50 ≈ 0.01 ms (budget ≤ 0.3 ms); `storage.store_batch` throughput, 10 000
  occurrences across 50 aggregates in ≈ 0.08 s (~120 000 occurrences/s).
