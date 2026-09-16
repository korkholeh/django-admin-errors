# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

Nothing yet.

## [0.1.1] — 2026-09-16

### Fixed

- **Naming the handler in `LOGGING` crashed at startup.** A project that wired the handler
  explicitly, as the README's *Explicit `LOGGING` wiring* section and the `admin_errors.W002`
  check both describe, got `ValueError: Unable to configure handler 'admin_errors'` from
  `django.setup()`. `setup()` calls `configure_logging()` before `apps.populate()`, so `dictConfig`
  constructed `AdminErrorsHandler` while the app registry was still empty — and `handlers.py`
  imported `admin_errors.capture` at module scope, which reaches `admin_errors.models`. The import
  now happens inside `emit()`, so the class costs nothing but the standard library to construct,
  and a record logged before the registry is ready is dropped instead of raising out of a logging
  handler. `AUTO_INSTALL_LOGGING_HANDLER` (the default) was never affected: `ready()` runs after
  the registry is populated.

  Reported against 0.1.0 by a project adopting the library. Pinned by a subprocess test, since any
  in-process test runs long after the registry is populated and would pass either way.

- README: the `LOGGING` snippet referenced a `console` handler it never defined, so copying it
  verbatim raised `ValueError: Unable to add handler 'console'`. It is now self-contained, and
  says in one line why W002 asks for this wiring.

## [0.1.0] — 2026-09-16

First release. Sentry-style error tracking inside the Django admin: unhandled exceptions and
error-level log records are captured, fingerprinted into issues, deduplicated and stored in the
host project's own database, with a changelist (summary cards, sparklines) and a detail page
(traceback, request context, occurrence history, Resolve/Ignore/Reopen). No external service, no
extra infrastructure, and exactly one runtime dependency: Django.

Install is `pip install django-admin-errors`, add `"admin_errors"` to `INSTALLED_APPS`, and
`python manage.py migrate`. No settings are required.

### Frozen contracts

Nothing breaks in a first release, but two things are contracts from this version onward, and
changing either is a breaking change:

- **The fingerprint algorithm** (ADR 0003). It decides which occurrences are the same issue;
  changing it duplicates every existing issue in every installation, so it is gated behind a major
  version bump and pinned by golden-value tests.
- **The event payload schema**, stored as a versioned JSON document with `"v": 1` (ADR 0004).
  Readers branch on `v` and tolerate missing keys, so old events stay readable after a schema change.

The public Python API is `admin_errors.api.{capture_exception, capture_message, flush}`,
`admin_errors.signals.*`, `admin_errors.admin.register(site)` and `admin_errors.__version__`.
Everything else is internal and may change within 0.x.

### Capture

- Errors are captured automatically from three entry points: `AdminErrorsHandler` on the root logger
  (installed from `AppConfig.ready()` unless `AUTO_INSTALL_LOGGING_HANDLER` is off), the public
  `capture_exception`/`capture_message` API, and Celery's `task_failure` signal. Optional
  `RequestContextMiddleware` attaches the current request to errors logged deep inside a view.
- Capture never raises and never recurses: the whole pipeline is wrapped in
  `try/except BaseException` (re-raising `KeyboardInterrupt`/`SystemExit`), with a thread-local
  recursion guard, a safe `repr()` that degrades to `<unrepresentable ClassName>`, and individually
  wrapped `BEFORE_SEND` and signal receivers.
- Scrubbing is delegated entirely to Django's own `get_exception_reporter_filter(request)`, so
  `@sensitive_variables`, `@sensitive_post_parameters` and a host's own filter all apply exactly as
  they do on Django's technical 500 page. POST fields are cleansed by key as well as by decorator,
  and `Authorization`-style headers are redacted identically on every supported Django version,
  4.2 included. Settings and environment are never stored.
- Guards drop what should not be recorded before any work happens: `ENABLED`, `CAPTURE_IN_DEBUG`,
  the level, `IGNORE_LOGGERS`, `IGNORE_EXCEPTIONS` (404 and `PermissionDenied` by default),
  `IGNORE_HTTP_STATUS_BELOW`, and anything logged under `admin_errors.` itself.
- A `BEFORE_SEND` hook can edit or drop any payload, and `MAX_PAYLOAD_BYTES` is enforced by
  degrading in a documented order: frame locals, then context lines, then the message, then a
  minimal payload.
- Text is sanitised for PostgreSQL — NUL bytes become U+FFFD and lone surrogates are stripped,
  recursively over every string value *and* key — so an exception message containing a NUL is still
  storable.

### Storage and the background writer

- `TRANSPORT="thread"` (the default) runs every database write in a lazily started daemon thread, so
  a request never blocks on error storage. `TRANSPORT="sync"` writes inline, for tests, debugging
  and single-worker deployments.
- The writer aggregates queued items by fingerprint and flushes on `FLUSH_BATCH_SIZE`, on
  `FLUSH_INTERVAL_SECONDS`, or on an explicit `api.flush()` (which really blocks until the queue is
  drained). `atexit` flushes with a 2 s timeout.
- Connection hygiene: `close_old_connections()` before every flush, one retry after an
  `OperationalError` before the batch is dropped and counted, close after 60 s idle, and an
  unconditional close when the thread stops.
- A fork is detected on every `enqueue()` via `os.getpid()`, so gunicorn `--preload` and uwsgi
  workers start a fresh thread instead of silently recording nothing into an inherited dead one.
- `storage.store_batch` uses one transaction per aggregate, so one bad row cannot lose a batch;
  every create that can race sits in its own savepoint, which is what makes it safe on PostgreSQL.
  `select_for_update` appears nowhere, because SQLite ignores it.
- `Issue.count` is the true total including unsampled occurrences, and `Issue.last_event` keeps the
  newest payload so the detail page still renders after event retention has deleted every `Event`.

### The Errors admin

- A Sentry-style changelist: three summary cards, status/level/exception-type/last-seen filters, a
  search across title, type and culprit, and a 14-day inline SVG sparkline per row served by one
  filtered `Prefetch` — 50 rows in at most 12 queries.
- A read-only detail page: traceback with library frames collapsed and per-frame locals behind a
  toggle, chained exceptions in CPython's root-cause-first order, the request block, a 30-day
  occurrence chart and table, and a *Copy as text* button that puts a plain-text traceback on the
  clipboard. At most 15 queries.
- Resolve / Ignore / Reopen as both single-issue buttons and bulk actions. `resolved_at` is kept on
  reopen, so an issue that comes back after being resolved shows a distinct **Regressed** badge
  rather than looking brand new.
- Sensitive context — headers, cookies, GET/POST/body, the user block, Celery args, frame locals,
  `extra` — is gated by the dedicated `admin_errors.view_issue_context` permission, enforced twice:
  stripped from the payload in the view before it reaches the template, and re-checked in every
  template include, including the *Copy as text* traceback.
- Registration is configurable: `ADMIN_SITE` takes a dotted path or an `AdminSite`, or `False` to
  skip auto-registration and call `admin_errors.admin.register(site)` by hand.
- 2.9 KB of CSS and 0.5 KB of JS, using only the admin's own CSS variables, so dark mode follows the
  host theme. No external requests. The locals toggle works with JavaScript disabled.
- All UI strings are translatable, with a hand-written Ukrainian catalogue (95 messages) shipped in
  `locale/uk/LC_MESSAGES`.

### Notifications and extension points

- `EmailNotifier` (the default `NOTIFY_BACKEND`) emails `NOTIFY_RECIPIENTS`, or `settings.ADMINS`,
  the first time a new issue appears and the first time a resolved issue regresses. Throttled per
  issue by `NOTIFY_THROTTLE_SECONDS` (one hour), so an error storm emails once.
- The throttle is also the concurrency contract: a single conditional `UPDATE` on
  `Issue.notified_at`, executed before the send, means only the racing process whose UPDATE touched
  a row sends. A failed send is not retried — the issue is still in the admin.
- Receivers are connected and disconnected dynamically by `notifications.refresh_connections()`, so
  a host with `NOTIFY_BACKEND=None` adds zero queries to the capture path.
- Three signals are the sanctioned extension point for anything beyond email: `issue_created`,
  `issue_regressed` and `issue_status_changed`. A receiver that raises is swallowed and counted,
  never propagated.

### Bounded storage

Four independent mechanisms, each cutting a different dimension (ADR 0006), so one failing does not
remove the ceiling: an admission bucket on new fingerprints (`NEW_ISSUES_PER_MINUTE`), per-fingerprint
payload sampling (`EVENT_SAMPLE_PER_HOUR`, with every occurrence still counted), a ring buffer of the
newest `EVENTS_PER_ISSUE` events, and `retention.run_cleanup()` — six ordered delete rules (stale
events, stale daily counts, resolved/ignored/open issue TTLs, `MAX_ISSUES` eviction with hysteresis)
plus an optional SQLite vacuum. Every delete is chunked at 1 000 ids, so cleanup never holds one long
write lock and an interrupted run is finished by the next one.

Cleanup runs opportunistically from the writer thread (at most once per `CLEANUP_INTERVAL_SECONDS`
per process, cache-locked across processes), from `manage.py errors_cleanup`, or from
`admin_errors.tasks.cleanup` under Celery beat.

### Operations

- `manage.py errors_test` captures one deliberate exception through the real pipeline and prints the
  resulting issue's admin URL, or names the likely cause when nothing was captured. This is the
  answer to "is it actually wired up?".
- `manage.py errors_stats` prints row counts, oldest/newest issue, a per-status breakdown, an
  approximate on-disk size, and the writer's process-local drop counters.
- `manage.py errors_cleanup` with `--dry-run`, `--vacuum` and `--database`.
- Five system checks turn silent misconfiguration into a startup message: `W001` (unknown
  `ADMIN_ERRORS` key), `W002` (`propagate: False` on `django.request` with no handler of ours —
  the most common reason nothing is ever captured), `W003` (`AdminEmailHandler` overlapping with
  `EmailNotifier`, so admins get two emails per error), `E001` (`DATABASE` names an alias not in
  `DATABASES`), `E002` (SQLite built without JSON1).
- `AdminErrorsRouter` (opt-in) pins the three models to a dedicated database alias — the real fix
  for SQLite write-lock contention with the rest of the host application.

### Compatibility and packaging

- Python 3.10–3.13 × Django 4.2, 5.2, 6.0, 6.1 — every combination Django itself supports. SQLite
  ≥ 3.9 (JSON1) and PostgreSQL ≥ 13, both first-class and both tested in CI.
- Django 4.2 reached upstream end of life in April 2026 and is kept as a CI-tested, best-effort
  target; Django's own security support for it is the host project's responsibility.
- `src/` layout, hatchling, MIT, exactly one shipped migration (`0001_initial`). Templates, static
  files and the `uk` catalogue are declared package data and proven present by installing the built
  wheel into a clean virtualenv and rendering from it (`tests/package_smoke.py`, run as the last
  step of `tox -e package`).
- Celery, psycopg, pytest, ruff and Playwright are extras or dev dependencies. The base install
  depends on Django and nothing else.

### Performance

Measured by `benchmarks/bench_capture.py` on an Apple Silicon Mac (Darwin 25.6.0), Python 3.13.3,
SQLite 3.49.1, `TRANSPORT="thread"` with a no-op sink. The harness exits non-zero when a budget is
missed.

| Scenario | p50 | p95 | Budget |
|---|---|---|---|
| capture, no locals | 1.309 ms | 1.392 ms | 2.0 ms |
| capture, with locals | 1.588 ms | 1.714 ms | 10.0 ms |
| capture, count-only | 0.013 ms | 0.014 ms | 0.3 ms |

`store_batch` throughput: 10,000 occurrences aggregated into 50 issues in 0.058 s
(≈ 173,600 occurrences/s).

### Known limits in this release

- Rate limiting is process-local by design, so sampling and admission budgets multiply by the number
  of worker processes.
- Resolving an issue does not reset its notification throttle: an issue that regresses inside the
  throttle window shows the **Regressed** badge but sends no second email.
- `notified_at` is set before the email is sent and a failed send is not retried, so a broken SMTP
  server loses that one notification.
- A `CAPTURE_LEVEL` below the host's root logger level makes `ready()` lower the root logger itself,
  which also makes those records reach the host's other root handlers. Raise the level on those
  handlers rather than relying on the root logger's level.
- Publishing to PyPI is not part of this release: the build and `twine check` are green, but nothing
  has been uploaded.

### Documentation

- `README.md` covers install, the full settings reference (40 keys), permissions and groups,
  retention and scheduling, the dedicated-alias and SQLite recipes, notifications and the
  `mail_admins` overlap, Celery/ASGI/gunicorn notes, the storage bound, and an FAQ that maps each
  silent-capture-failure mode onto the command or check that diagnoses it.
- `docs/user/` is the operator's guide (getting started, triage, retention, notifications,
  troubleshooting); `docs/dev/architecture.md` and `docs/dev/adr/` are the developer's;
  `docs/spec.md` is the implementation specification.
- `tests/test_docs.py` pins these claims against the code — the settings table's keys and defaults,
  the screenshots, the dependency list, exactly one migration, no `# pragma: no cover` on
  capture/storage paths — so a stale doc fails the suite.

### Fixed before release

Bugs found and fixed during development. None of them ever shipped; they are recorded because each
one marks a trap worth knowing about.

- `includes/occurrences.html` passed `event.payload.celery.task` as a filter *argument* to
  `|default:`, which Django resolves without swallowing a missing key — so any event without a
  `celery` block, i.e. every plain HTTP capture, returned a 500 for the whole issue detail page.
  Fixed with `{% firstof %}`.
- The detail page's Resolve/Ignore/Reopen buttons were `<form>` elements nested inside the admin's
  own change `<form>` — invalid HTML that browsers silently misparse, so clicking Resolve submitted
  to the change-form URL instead of the transition URL. Only reachable from a real browser, not from
  `Client()` tests. Fixed with `formaction`/`formmethod` buttons.
- `Writer._run()` closed its database connection only after the idle timeout, so a thread that
  stopped right after a flush leaked a PostgreSQL server-side session until garbage collection —
  visible as an intermittent `DROP DATABASE` failure at the end of a PostgreSQL run.
- `retention._evict_status` deleted without chunking, so a large eviction could hold one long lock.
- `demo_seed --issues N` silently capped at 64 distinct issues while reporting `N` as created; it
  now fails with a clear error above its budget.
- `demo/`'s sensitive form was missing the `token` input the manual QA script describes, so the
  `@sensitive_variables("token")` scrub path was unreachable from a browser.
- `make e2e-up` joined its setup steps with `;` instead of failing fast, so a broken
  `migrate`/`demo_seed`/`playwright install` reported a misleading "server never became ready".
- The issue detail page's badge row and status buttons sat 10px left of every line of text above
  them, because the admin pads `p` and `dl` inside `.module` but leaves `div` alone — and Delete,
  wrapped in a `p`, sat on a line of its own, indented differently from the three buttons it
  belongs with. They are now one padded flex row. Delete had also been given `border-color` and
  `color` that never applied: Django's own `a.button` selector outranks a bare class, so the link
  kept the same solid blue fill as Resolve and was re-padded smaller than its neighbours.
- The end-to-end storm case passed on a fresh demo server and failed on every run after it against
  the same process. `EVENT_SAMPLE_PER_HOUR` is a per-fingerprint, process-local token bucket that
  deleting the issue does not reset, so the second storm stored counters and no events — correct
  behaviour (see *Bounded storage*), wrongly asserted against. `/storm/` takes an optional `?tag=`
  that fingerprints a burst on its own, and the case passes a fresh one per run.
- The README screenshots were captured from a minutes-old issue whose occurrence chart was a
  single bar, with light and dark taken from two different moments, and the full-page list ran to
  3500px. `demo_seed --backfill-history` now gives an existing issue a history without touching its
  events, both themes are captured from one state, and the list is cropped to the viewport. The
  demo also sets `BEFORE_SEND` to shorten absolute paths and pin the hostname, which keeps the
  screenshots free of a developer's home directory and doubles as the worked example of that hook.
- The test suite failed on Python 3.10 (`tests/test_docs.py` imported `tomllib`, stdlib only from
  3.11; it now falls back to `tomli`) and on Django 6.1, where `run_checks()` opens a database
  connection through `JSONField._check_supported` and an overridden `LOGGING` is fed straight to
  `dictConfig`. The library itself behaved identically on every version throughout.
