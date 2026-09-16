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
- `admin_errors.retention.run_cleanup` (spec section 9.3): the six bounded-storage rules (stale
  events, stale daily counts, resolved/ignored/open issue TTLs, `MAX_ISSUES` eviction with
  hysteresis, oldest-`last_seen`-first across ignored → resolved → open) plus an optional SQLite
  `VACUUM`/`PRAGMA incremental_vacuum`, all chunked at 1000 ids per delete so a cleanup never holds
  one long write lock. `--dry-run` reports every rule's count without deleting anything. Triggered
  from three places: the `errors_cleanup` command, the writer's opportunistic hook (at most once
  per `CLEANUP_INTERVAL_SECONDS` per process, cache-locked against other processes, disabled by
  `CLEANUP="off"`), and the new `admin_errors.tasks.cleanup` Celery task.
- Management commands: `errors_cleanup` (`--vacuum`, `--dry-run`, `--database`), `errors_stats`
  (row counts, oldest/newest issue, per-status counts, approximate on-disk size, writer stats), and
  `errors_test` (captures one real exception through the public API and prints the resulting
  issue's admin URL, or a `CommandError` naming the likely cause when nothing was captured — the
  "is it actually wired up?" instrument for risk 12).
- `admin_errors.routers.AdminErrorsRouter`: an optional database router that pins the three
  `admin_errors` models to `ADMIN_ERRORS['DATABASE']`, for hosts that want error storage on a
  dedicated alias (spec section 11.3). Not installed by default.
- System checks `admin_errors.E001` (`ADMIN_ERRORS['DATABASE']` names an alias that is not in
  `settings.DATABASES`) and `admin_errors.W002` (`django.request`/`django` logger configured with
  `propagate: False` and no `AdminErrorsHandler` in its own handler list, so captures never
  happen).
- Optional Celery integration: `admin_errors.integrations.celery` connects to the `task_failure`
  signal (behind `importlib.util.find_spec("celery")`, so a host without Celery installed is
  unaffected) and captures the failure with a `celery` context block (task name, task id,
  truncated `args`/`kwargs`) alongside the exception's own context.
- PostgreSQL pass: `demo/docker-compose.yml` (a `postgres:16` service matching the CI `postgres`
  job's DSN, pinned by a `tests/test_toolchain.py` contract test) plus `make pg-up`/`pg-down`/
  `test-pg` (bring the container up, run the full suite under `DJANGO_DB=postgres`, always tear
  down). The full suite, including the two-thread same-fingerprint race and the storage
  `assertNumQueries` budgets, is green on PostgreSQL with no `storage.py`/`retention.py` savepoint
  change needed — that discipline held since Phase 3. `admin_errors.writer.Writer._run()` now
  closes its own database connection when the thread shuts down, not only after
  `IDLE_CONNECTION_SECONDS` of subsequent idleness: `stop()` could exit the loop immediately after
  a flush, leaking a real PostgreSQL server-side session until garbage collection happened to close
  it, which surfaced as an intermittent `DROP DATABASE` failure at the end of a PostgreSQL test run.
  New `tests/test_postgres.py`
  (`postgres_only` fixture in `conftest.py`) asserts the two savepointed creates leave their outer
  transaction usable after a swallowed `IntegrityError`, a JSONB payload round-trips non-ASCII and
  emoji, and `context.sanitize_text()`/`context.sanitize_payload()` (new: replaces NUL with U+FFFD,
  strips lone surrogates, applied recursively over every string value *and* key in the payload,
  called once from `capture._build_and_store` after the `extra` merge and `BEFORE_SEND`, and to
  `capture.py`'s `meta` strings) keep an exception message — or a query-param/header/`extra` key —
  containing a NUL byte storable on PostgreSQL. `tests/test_storage.py` gained
  `test_last_seen_never_moves_backwards`, pinning the `Greatest(F("last_seen"), ...)` contract on
  both backends.
- Demo project (spec section 13): `demo/` is a real, unpublished Django project
  (`demo/manage.py`, `demo_project/`, `demo_app/`) that exercises every capture behaviour by hand
  and becomes the browser surface for later phases. Thirteen views (`/`, `/boom/`,
  `/boom/<int:n>/`, `/keyerror/<slug>/`, `/nested/`, `/logged/`, `/warning/`, `/sensitive/`,
  `/storm/`, `/unique-storm/`, `/task/`, `/async-boom/`, `/404/`) cover unhandled exceptions,
  logged errors, scrubbing, aggregation and admission limits; `demo_app.tasks.fail_task` runs as a
  Celery task when `celery` is installed and as a direct call otherwise, so `/task/` produces one
  issue either way. `demo_app.management.commands.demo_seed --issues N --days D [--reset]`
  generates deterministic data (fixed exception/culprit pairs, `random.Random` fixed seed) through
  the real capture pipeline — same `(fingerprint, count, status, daily counts)` snapshot across
  repeated `--reset` runs — and creates (or resets the password of) a superuser `admin`/`admin`.
  `make demo` / `make demo-pg` migrate, seed and serve; `make e2e-up` now really starts the server
  (migrate, seed `--reset`, install chromium, background `runserver`, poll the ready URL) instead
  of no-op'ing, and `make e2e-down` tears it down; a new `e2e/test_admin_login.py` logs in through a
  real browser and reaches the admin index. `tests/test_demo.py` drives the full URL map, the
  storm/unique-storm aggregation and admission bounds, and `demo_seed`, added to the main gate
  (`pythonpath = [".", "demo"]`); `demo/` and `tests/test_demo.py` stay out of the sdist.
- e2e plans and specs (`e2e/plans/admin-login.plan.yaml`, `e2e/plans/demo-app-surface.plan.yaml`):
  the login case gained a wrong-password rejection and a session-persists-on-reload case; a new
  `e2e/test_demo_surface.py` drives the demo's own pages through a real browser (the index page's
  links, the sensitive form's rendered fields, and the technical 500/404 debug pages that
  `tests/test_demo.py` only ever sees as a status code). `e2e/conftest.py` now writes
  `e2e/RESULTS.md` after every run and defaults to `--screenshot=only-on-failure`.
- Admin UI (spec section 12): `admin_errors.admin.IssueAdmin`, registered on `ADMIN_SITE`
  (`django.contrib.admin.site` by default; a dotted path, an `AdminSite` instance/class, or `False`
  to opt out — `admin_errors.admin.register(site)` is the public entry point) with a Sentry-style
  changelist (three summary cards, status/level/exception-type/last-seen filters, a 14-day inline
  SVG sparkline per row via one filtered `Prefetch`, ≤ 12 queries for 50 issues) and a read-only
  detail page (traceback with collapsed library frames and toggleable locals, chained-exception
  separators in CPython's own root-cause-first order, request block, a 30-day occurrences chart and
  table, server info; ≤ 15 queries). Resolve/Ignore/Reopen as both single-issue buttons and bulk
  actions, sharing one `_apply_status()` idempotent-update helper and firing the (until now unused)
  `issue_status_changed` signal through `signals.send_safely()`; `resolved_at` is kept on reopen so
  a re-triggered issue shows a "Regressed" badge. Sensitive context (headers, cookies, GET/POST/
  body, the user block, Celery args/kwargs, frame locals, `extra`) is gated twice — once in the view
  (stripped from the payload before it reaches the template) and once per template include — behind
  `view_issue_context`, independent of `view_issue`/`change_issue`/`delete_issue`. New
  `templatetags/admin_errors_tags.py` (sparkline/bar-chart SVG, status/level badges, traceback-block
  ordering, compact numbers, frame labels, safe dict-table rendering) and
  `static/admin_errors/{admin_errors.css,admin_errors.js}` (2.9 KB / 0.5 KB, well under the 6 KB /
  3 KB budgets; only the admin's own CSS variables, so dark mode follows the host; vanilla JS that
  only enhances — the locals `<details>` toggle works with JS disabled; expanding a collapsed
  library frame's context requires JS (it stays collapsed without it), and the toggle `<button>`
  carries `aria-expanded`, kept in sync on click).
  `demo_seed` now also creates a `viewer`/`viewer` staff user holding only `view_issue`, so the
  permission-gated case is reachable from a real browser. `e2e/plans/admin-ui.plan.yaml` and
  `e2e/test_admin_ui.py` cover the list, detail, permission-gating and resolve/re-hit/regressed
  flows through a real Chromium session; `e2e/test_screenshots.py` produces the four
  `docs/img/issue-{list,detail}-{light,dark}.png` README screenshots.
- Email notifications (spec section 10): `admin_errors.notifications.EmailNotifier`, the default
  `NOTIFY_BACKEND`, sends one plain-text `send_mail` per issue creation/regression to
  `NOTIFY_RECIPIENTS` (falling back to `django.conf.settings.ADMINS`). A single conditional `UPDATE`
  on `Issue.notified_at`, executed *before* the send, both throttles repeat notifications
  (`NOTIFY_THROTTLE_SECONDS`) and is the concurrency contract: only the racer whose `UPDATE` touches
  a row sends, proved by a two-thread test. `refresh_connections()` connects/disconnects the
  `issue_created`/`issue_regressed` receivers dynamically (only when `NOTIFY_BACKEND` resolves, the
  matching reason is in `NOTIFY_ON`, and the backend class's optional `is_enabled()` hook returns
  `True` — absent hook means enabled; `EmailNotifier.is_enabled()` is the one that requires a
  non-empty recipient list), so the default test host and any host with `NOTIFY_BACKEND=None` add
  zero queries to the capture path and every existing `assertNumQueries` budget stays unmoved. `conf.py` gains `NOTIFY_BASE_URL` (prefixed to the admin change-view link in
  the mail body). New `admin_errors.textformat.format_traceback_text()` renders a CPython-style
  plain-text traceback (innermost frame last, chained exceptions root-cause-first, optional locals,
  capped frame count) shared by the notification body and the admin's new "Copy as text" button
  (`includes/traceback.html`, `admin_errors.js`'s `onCopyClick`, Clipboard API with an
  `execCommand("copy")` fallback), gated the same way as the traceback itself (redacted payload +
  `include_locals=view_issue_context`).
- System check `admin_errors.W003`: warns when `NOTIFY_BACKEND` is set and the host's `LOGGING`
  config also routes to an `AdminEmailHandler`, since both would mail the same operators about the
  same errors.
- Ukrainian translation catalogue (`locale/uk/LC_MESSAGES/django.{po,mo}`, 95 `msgid`s), completing
  the i18n sweep started in Phase 8; a `.po`-parsing test asserts every `msgid` has a non-empty
  `msgstr` and admin pages render under `LANGUAGE_CODE="uk"`.
- `e2e/plans/notifications-i18n.plan.yaml` + `e2e/test_notifications_i18n.py`: a real demo server
  (console `EMAIL_BACKEND`) writes a "New issue:" notification to its own stdout, a resolve/re-hit
  cycle flips the status badge to "Regressed", and "Copy as text" flips its label after a real
  Clipboard API write.

### Fixed

- `demo/demo_app/templates/demo_app/sensitive_form.html` was missing the `token` input described
  by the manual QA script, so the `@sensitive_variables("token")` scrub path was not reachable from
  a browser; the view now reads a posted `token` field.
- `demo_seed --issues N` silently capped at 64 distinct issues (its exception × culprit budget)
  while reporting the requested `N` as if it had been created; it now raises a clear `CommandError`
  above that budget and reports the actual number of issues created.
- `make e2e-up`'s setup steps were joined with `;` instead of failing fast, so a broken
  `migrate`/`demo_seed`/`playwright install` still ran the full 45 s ready-poll and reported a
  misleading "server never became ready" instead of the real error.
- `make demo`/`make demo-pg` now seed with `--reset`, matching `make e2e-up`, so repeated runs
  reproduce the same 40-issue surface instead of accumulating events across runs.
- README's storm/boom walkthrough now describes the writer's bounded queue instead of promising an
  exact `+5000` occurrence count that a burst can legitimately fall short of.
- `includes/occurrences.html`'s Path/task column used `event.payload.celery.task` as a filter
  *argument* (`|default:`), which Django resolves without swallowing a missing key; any event
  without a `"celery"` key — i.e. every plain HTTP capture — 500'd the whole issue detail page.
  Fixed with `{% firstof %}`, Django's own safe "try A, then B, then a literal fallback" primitive.
- The admin detail page's Resolve/Ignore/Reopen buttons lived in `<form>` elements nested inside the
  admin's own outer change-form `<form>` — invalid HTML that a browser silently misparses, so
  clicking "Resolve" actually submitted to the change-form URL instead of the status-transition one
  (only reachable from a real browser, not from `Client()`-based tests). Fixed with `formaction`/
  `formmethod` buttons instead of nested forms.
