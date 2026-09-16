# django-admin-errors

Sentry-style error tracking, built into the Django admin. Unhandled exceptions and error-level log
records are captured into your own project's database — grouped into issues by fingerprint,
deduplicated, with traceback, request context, counts and 14/30-day trends. No external service, no
extra infrastructure: just your database and Django's own admin.

Use it when Sentry (or an equivalent SaaS) is overkill, not allowed by policy, or not worth the
operational cost for a small project. It is not a replacement for Sentry at scale: there is no
alerting pipeline, no release tracking, no cross-project search — see *Non-goals* below.

## Status

Released as `0.1.0`. See `CHANGELOG.md` for the full history.

## Screenshots

Issue list, with per-issue sparklines and summary cards:

![Issue list, light theme](docs/img/issue-list-light.png)
![Issue list, dark theme](docs/img/issue-list-dark.png)

Issue detail, with traceback, request context and occurrence history:

![Issue detail, light theme](docs/img/issue-detail-light.png)
![Issue detail, dark theme](docs/img/issue-detail-dark.png)

## Compatibility

| Python | Django |
|---|---|
| 3.10 | 4.2, 5.2 |
| 3.11 | 4.2, 5.2 |
| 3.12 | 4.2, 5.2, 6.0, 6.1 |
| 3.13 | 5.2, 6.0, 6.1 |

Django 4.2 is upstream end-of-life but is kept as a CI-tested, best-effort target because the project
targets it as a floor; Django's own security support for 4.2 is the host project's responsibility, not
this package's. Databases: SQLite ≥ 3.9 (JSON1) and PostgreSQL ≥ 13. Zero runtime dependencies beyond
Django itself.

## Install

```sh
pip install django-admin-errors
```

Add the app and migrate:

```python
INSTALLED_APPS = [
    # ...
    "django.contrib.admin",
    "admin_errors",
]
```

```sh
python manage.py migrate
```

That's it for the default configuration: unhandled view exceptions are captured automatically,
because `admin_errors` installs a logging handler on the root logger from `AppConfig.ready()`
(`AUTO_INSTALL_LOGGING_HANDLER`, on by default) at `ADMIN_ERRORS["CAPTURE_LEVEL"]` (`"ERROR"`).

Two optional steps:

- **Request context on manually logged errors.** Add `RequestContextMiddleware` so
  `logger.exception(...)` calls made outside of Django's own exception handling (e.g. in a
  background thread started from a view, or a `try`/`except` around business logic) still capture
  the originating request:

  ```python
  MIDDLEWARE = [
      # ...
      "admin_errors.middleware.RequestContextMiddleware",
  ]
  ```

- **Explicit `LOGGING` wiring**, useful when a project already customizes `LOGGING` and wants to see
  exactly how `admin_errors` fits in (equivalent to what `AUTO_INSTALL_LOGGING_HANDLER` does
  automatically):

  ```python
  LOGGING = {
      "version": 1,
      "disable_existing_loggers": False,
      "handlers": {
          "admin_errors": {"class": "admin_errors.handlers.AdminErrorsHandler", "level": "ERROR"},
      },
      "root": {"handlers": ["console", "admin_errors"], "level": "WARNING"},
  }
  ```

  If you set this explicitly, also set `ADMIN_ERRORS = {"AUTO_INSTALL_LOGGING_HANDLER": False}` to
  avoid a second, redundant handler instance.

Set `ADMINS` and an `EMAIL_BACKEND` if you want an email the first time a new issue appears (see
*Notifications and signals* below; on by default, but silent without at least one address in
`ADMINS` or `ADMIN_ERRORS["NOTIFY_RECIPIENTS"]`):

```python
ADMINS = [("Ops", "ops@example.com")]
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
```

Then verify the whole pipeline end to end — capture, storage, admin — with one command:

```sh
python manage.py errors_test
```

This raises one deliberate, harmless exception through the real `capture_exception()` /
`flush()` path and prints the resulting issue's admin URL. If it fails, it also prints a diagnosis
(see the FAQ below for the same checks in more detail).

## How it works

```
raise / logger.error(...)
        │
        ▼
  capture (admin_errors.capture)
    - guards: ENABLED, DEBUG, level, ignored loggers/exceptions, HTTP status  ── drops here
    - scrub (Django's own get_exception_reporter_filter)
    - fingerprint (admin_errors.fingerprint)
    - BEFORE_SEND hook                                                        ── may drop or edit
    - MAX_PAYLOAD_BYTES degradation (vars → context lines → truncated message)
        │
        ▼
  queue (TRANSPORT="thread") or inline call (TRANSPORT="sync")
        │
        ▼
  writer thread (admin_errors.writer)
    - NEW_ISSUES_PER_MINUTE admission limit                                   ── drops here
    - EVENT_SAMPLE_PER_HOUR per-fingerprint sampling                          ── counts, doesn't store
        │
        ▼
  storage.store_batch → Issue / Event / IssueDailyCount (PostgreSQL-safe savepoints)
        │
        ▼
  Django admin: issue list (sparklines, counts) and issue detail (traceback, request, occurrences)
```

Everything above the writer runs synchronously in the thread that raised or logged; everything from
the writer down runs in a background daemon thread by default (`TRANSPORT="thread"`), so a request
never blocks on a database write. `TRANSPORT="sync"` writes inline instead, which is what the test
suite uses by default and is useful for debugging or single-worker deployments.

## Settings

All settings live in one dict `ADMIN_ERRORS` in the host project's `settings.py`. Unknown keys raise
a system-check warning (`admin_errors.W001`).

| Key | Default | Meaning |
|---|---|---|
| `ENABLED` | `True` | Master switch. When `False`, capture is a no-op; admin still works. |
| `DATABASE` | `"default"` | DB alias used for all reads/writes. See *Retention* below for a dedicated alias. |
| `TRANSPORT` | `"thread"` | `"thread"` (background writer) or `"sync"` (write inline; used in tests and for debugging). |
| `CAPTURE_LEVEL` | `"ERROR"` | Minimum log level captured by the logging handler. |
| `AUTO_INSTALL_LOGGING_HANDLER` | `True` | In `ready()`, attach `AdminErrorsHandler` to the root logger if not already attached. |
| `CAPTURE_IN_DEBUG` | `True` | Capture when `settings.DEBUG` is `True`. |
| `IGNORE_LOGGERS` | `["django.security.DisallowedHost"]` | Logger names (exact or prefix with trailing `.`) never captured. |
| `IGNORE_EXCEPTIONS` | `["django.http.Http404", "django.core.exceptions.PermissionDenied"]` | Dotted paths; subclasses also ignored. |
| `IGNORE_HTTP_STATUS_BELOW` | `500` | Records from `django.request` / `django.server` carrying `status_code` below this are dropped regardless of level. |
| `BEFORE_SEND` | `None` | Dotted path to `callable(payload: dict, hint: dict) -> dict \| None`. Return `None` to drop. Runs in the capturing thread. |
| `IN_APP_INCLUDE` | `None` | List of path prefixes considered in-app. Default: `settings.BASE_DIR` if defined. |
| `IN_APP_EXCLUDE` | `["site-packages", "dist-packages", "/lib/python"]` | Substrings marking non-app frames. |
| `CAPTURE_LOCALS` | `True` | Store local variables for sampled events (scrubbed). |
| `CAPTURE_REQUEST_BODY` | `False` | Store raw request body (truncated to `MAX_BODY_BYTES`). |
| `MAX_BODY_BYTES` | `4096` | Truncation limit for the stored request body. |
| `MAX_VAR_REPR_LENGTH` | `200` | Truncate every variable repr. |
| `MAX_FRAMES` | `50` | Keep innermost N frames (in-app frames are not prioritised; simply keep the last N). |
| `MAX_PAYLOAD_BYTES` | `65536` | Hard cap on serialized payload; if exceeded, drop `vars`, then context lines, then truncate message. |
| `EVENTS_PER_ISSUE` | `20` | Ring buffer of stored events per issue. |
| `EVENT_SAMPLE_PER_HOUR` | `10` | Per process, per fingerprint: full payloads stored per hour; beyond that only counters. |
| `NEW_ISSUES_PER_MINUTE` | `50` | Per process: unseen fingerprints admitted per minute; beyond that the event is dropped and counted in internal stats. |
| `QUEUE_MAXSIZE` | `1000` | Writer queue; overflow drops the newest item. |
| `FLUSH_INTERVAL_SECONDS` | `1.0` | Writer aggregation window. |
| `FLUSH_BATCH_SIZE` | `200` | Flush early when the aggregation dict reaches this many items. |
| `EVENT_RETENTION_DAYS` | `30` | Delete events older than this. |
| `DAILY_COUNT_RETENTION_DAYS` | `90` | Delete daily counters older than this. |
| `RESOLVED_ISSUE_TTL_DAYS` | `14` | Delete resolved issues whose `resolved_at` and `last_seen` are older than this. |
| `OPEN_ISSUE_TTL_DAYS` | `90` | Delete open issues not seen for this long. |
| `IGNORED_ISSUE_TTL_DAYS` | `None` | `None` = keep ignored issues (they only get counter updates); eviction may still remove them. |
| `MAX_ISSUES` | `5000` | Hard cap; eviction order below. |
| `CLEANUP` | `"opportunistic"` | `"opportunistic"` (writer thread runs cleanup ≤ once per `CLEANUP_INTERVAL_SECONDS`), or `"off"` (only command / Celery task). |
| `CLEANUP_INTERVAL_SECONDS` | `3600` | Minimum gap between opportunistic cleanup runs, per process. |
| `SQLITE_VACUUM` | `"incremental"` | `"incremental"` runs `PRAGMA incremental_vacuum` after cleanup when `auto_vacuum=2`; `"off"` never. Full `VACUUM` only via `errors_cleanup --vacuum`. |
| `NOTIFY_BACKEND` | `"admin_errors.notifications.EmailNotifier"` | Dotted path or `None`. |
| `NOTIFY_RECIPIENTS` | `None` | `None` → `settings.ADMINS` emails. |
| `NOTIFY_THROTTLE_SECONDS` | `3600` | Minimum gap between notifications for the same issue. |
| `NOTIFY_ON` | `["created", "regressed"]` | Subset of `created`, `regressed`. |
| `NOTIFY_BASE_URL` | `""` | Prefix for the admin URL included in notification emails (e.g. `"https://example.com"`). |
| `ADMIN_SITE` | `None` | Dotted path to an `AdminSite` instance to register with. `None` → `django.contrib.admin.site`. Set to `False` to skip auto-registration and call `admin_errors.admin.register(site)` manually. |
| `INTERNAL_LOGGING` | `False` | Log internal problems (dropped items, DB failures) to logger `admin_errors.internal` at WARNING. Off by default to avoid noise. |

`conf.settings` re-reads the dict on Django's `setting_changed` signal, so `override_settings` works
in tests. Always read settings through `admin_errors.conf.settings` if you're writing code against
this package — not `django.conf.settings.ADMIN_ERRORS` directly.

## Permissions and groups

| Permission | Grants |
|---|---|
| `admin_errors.view_issue` | list, detail without sensitive context, traceback without locals |
| `admin_errors.view_issue_context` | request headers/cookies/GET/POST/user, Celery args, frame locals, `extra` |
| `admin_errors.change_issue` | resolve / ignore / reopen (actions and buttons) |
| `admin_errors.delete_issue` | delete issues (cascade deletes events and daily counts) |
| `admin_errors.add_issue` | unused; adding issues through the admin is always disabled |

Superusers pass all checks. **`view_issue_context` is the sensitive one**: it is what exposes request
headers, cookies, POST data, the authenticated user, Celery task args and frame locals — the same
class of data Django's own technical 500 page shows. It is gated in both the view and the template
that renders the "Copy as text" traceback block, not just one or the other.

A "triage" group that can read and resolve issues, but not see sensitive context or delete anything:

```python
from django.contrib.auth.models import Group, Permission

group, _ = Group.objects.get_or_create(name="Error triage")
group.permissions.set(
    Permission.objects.filter(
        content_type__app_label="admin_errors",
        codename__in=["view_issue", "change_issue"],
    )
)
```

## Retention

Four independent limiters keep storage bounded (spec section 9, `admin_errors.retention.run_cleanup`),
run in this order:

1. Delete `Event` rows older than `EVENT_RETENTION_DAYS` (default 30 days).
2. Delete `IssueDailyCount` rows older than `DAILY_COUNT_RETENTION_DAYS` (default 90 days).
3. Delete resolved issues whose `resolved_at`/`last_seen` are older than `RESOLVED_ISSUE_TTL_DAYS`
   (default 14 days).
4. Delete ignored issues not seen in `IGNORED_ISSUE_TTL_DAYS` (default `None` — kept indefinitely,
   though eviction below may still remove them), then open issues not seen in `OPEN_ISSUE_TTL_DAYS`
   (default 90 days).
5. Evict the oldest issues (by `last_seen`, in order ignored → resolved → open) once the total
   exceeds `MAX_ISSUES` (default 5000), down to 90% of the cap (hysteresis, to avoid evicting on
   every single write once at the limit).

Run it by hand, always with `--dry-run` first:

```sh
python manage.py errors_cleanup --dry-run
python manage.py errors_cleanup
python manage.py errors_cleanup --vacuum   # also runs a full SQLite VACUUM; locks the DB, avoid under load
```

Or on a schedule, opportunistically (the default: the writer thread runs cleanup at most once per
`CLEANUP_INTERVAL_SECONDS`, itself gated by a cache lock so multiple processes don't collide), via
Celery beat:

```python
CELERY_BEAT_SCHEDULE = {
    "admin-errors-cleanup": {
        "task": "admin_errors.cleanup",
        "schedule": 3600.0,  # seconds; admin_errors.tasks.cleanup only exists if celery is importable
    },
}
```

or a plain cron entry running `python manage.py errors_cleanup` daily.

### Dedicated database alias

For SQLite projects especially, pointing `admin_errors` at its own database file avoids write-lock
contention with the rest of the app and makes a full `VACUUM` cheap:

```python
DATABASES["errors"] = {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "errors.sqlite3"}
DATABASE_ROUTERS = ["admin_errors.routers.AdminErrorsRouter"]
ADMIN_ERRORS = {"DATABASE": "errors"}
```

```sh
python manage.py migrate --database=errors
```

The router (`AdminErrorsRouter`) only ever opines about `admin_errors` models, so it's safe to add
alongside a host's own routers. System check `admin_errors.E001` errors if the alias isn't a key of
`DATABASES`.

### SQLite tips

- Enable WAL mode so reads (the admin) don't block on writes (capture):
  `PRAGMA journal_mode=WAL;` once, via a migration or a `post_migrate` hook.
- Set a busy timeout to ride out brief lock contention instead of raising `OperationalError`:
  `DATABASES["default"]["OPTIONS"] = {"timeout": 20}`.
- For incremental space reclamation without a full-file rewrite, set
  `PRAGMA auto_vacuum=2;` (`INCREMENTAL`) once on a fresh database and leave
  `ADMIN_ERRORS["SQLITE_VACUUM"] = "incremental"` (the default) — cleanup then runs
  `PRAGMA incremental_vacuum` for you.

## Notifications and signals

By default, the first occurrence of a new issue (and the first occurrence after a resolved issue
regresses) sends one email via `EmailNotifier`, throttled per issue by `NOTIFY_THROTTLE_SECONDS`
(default one hour) so a storm of the same error only emails once. Recipients are
`ADMIN_ERRORS["NOTIFY_RECIPIENTS"]` or, if unset, `settings.ADMINS`. Turn it off entirely with
`ADMIN_ERRORS = {"NOTIFY_BACKEND": None}`, or narrow it with `NOTIFY_ON = ["created"]` to skip
regression emails.

Three signals are the sanctioned extension point for anything beyond email —
`admin_errors.signals.issue_created`, `issue_regressed` and `issue_status_changed`. Receivers of the
first two run in the writer thread (or inline under `TRANSPORT="sync"`); exceptions raised by a
receiver are swallowed and counted, never propagated:

```python
from django.dispatch import receiver
from admin_errors.signals import issue_created


@receiver(issue_created)
def post_to_chat(sender, issue, event, **kwargs):
    send_to_slack(f"New issue: {issue.title} ({issue.culprit})")
```

### Replacing `mail_admins`

Django's built-in `AdminEmailHandler` (the one behind `LOGGING`'s implicit `mail_admins` handler on
`django.request`) emails admins on **every** occurrence of an error, with no deduplication or
throttling — the exact problem this package's `EmailNotifier` solves. If your `LOGGING` still
references `AdminEmailHandler`, remove it once `admin_errors` is installed:

```python
LOGGING = {
    # ...
    "loggers": {
        "django.request": {"handlers": ["admin_errors"], "level": "ERROR", "propagate": False},
    },
}
```

System check `admin_errors.W003` warns when both `AdminEmailHandler` and `EmailNotifier` are active
at once, since that means every error-level record emails admins twice. A queued email backend
(Celery, `django-anymail`'s async senders, or similar) is recommended in production so a slow SMTP
call in the writer thread never delays the next flush.

## Celery / ASGI / gunicorn

- **Celery**: task failures are captured automatically when `celery` is importable —
  `admin_errors.integrations.celery` connects to Celery's own `task_failure` signal, with no
  Celery-specific settings needed. `TRANSPORT` still governs how the resulting event is stored;
  `CELERY_TASK_ALWAYS_EAGER=True` (common in dev) works the same as a real worker.
- **gunicorn `--preload`**: the writer thread starts lazily on the first `enqueue()`, never from
  `AppConfig.ready()`, so it is safe under `--preload` (which runs `ready()` once in the master
  before forking) — each worker process starts its own thread on first use and a fork is detected on
  every `enqueue()` (`os.getpid()` check) so a stale inherited thread is never reused.
- **`runserver`'s autoreloader**: for the same reason (no thread started at import time), the
  autoreloader's double-import of `ready()` never leaves two writer threads running.
- **ASGI / async views**: `RequestContextMiddleware` is `sync_capable` and `async_capable`, and uses
  a `ContextVar` — asgiref propagates context vars into `sync_to_async` threads, so a sync view
  running under ASGI still keeps its request context. An `async def` view that raises is captured
  the same way a sync one is.
- **Shutdown**: call `admin_errors.api.flush()` before your process exits (e.g. in a gunicorn
  `worker_exit` hook) to block until the writer's queue is drained, so a burst just before shutdown
  isn't lost. It is a no-op under `TRANSPORT="sync"` or if the writer thread was never started.

## Storage bound and benchmarks

With the defaults, `Issues ≤ MAX_ISSUES` (5000) and `Events ≤ MAX_ISSUES × EVENTS_PER_ISSUE`
(5000 × 20 = 100,000) with each event's payload ≤ `MAX_PAYLOAD_BYTES` (64 KB) — a theoretical worst
case of ≈ 6.4 GB, but in practice tens of MB, because most issues have 1–3 stored events and real
payloads run 5–15 KB. `IssueDailyCount` adds at most
`MAX_ISSUES × DAILY_COUNT_RETENTION_DAYS` rows (5000 × 90 = 450,000) of about 40 bytes each — under
20 MB. Run `python manage.py errors_stats` for the actual numbers on your database.

Benchmarks (`benchmarks/bench_capture.py`), measured on an Apple Silicon Mac (macOS, Darwin kernel
25.6.0), Python 3.13.3, SQLite 3.49.1:

| Scenario | p50 | p95 | Budget |
|---|---|---|---|
| capture, no locals | 1.309 ms | 1.392 ms | 2.0 ms |
| capture, with locals | 1.588 ms | 1.714 ms | 10.0 ms |
| capture, count-only | 0.013 ms | 0.014 ms | 0.3 ms |

`store_batch` throughput: 10,000 occurrences aggregated into 50 issues in 0.058 s (≈ 173,600
occurrences/s). See `CHANGELOG.md`'s `[0.1.0]` entry for the run this table was copied from.

## FAQ

**Nothing appears in the admin.** Run `python manage.py errors_test` first — it prints exactly why
capture failed. Common causes, in order of likelihood:

- `LOGGING` sets `propagate: False` on `django.request` (or `django`) without listing
  `admin_errors` among that logger's own handlers — check `admin_errors.W002` (`python manage.py
  check`).
- `DEBUG=True` and `ADMIN_ERRORS["CAPTURE_IN_DEBUG"]` was explicitly set to `False`.
- `ADMIN_ERRORS["CAPTURE_LEVEL"]` is set above what your code actually logs at (e.g. `"ERROR"` while
  you only call `logger.warning(...)`).
- `ADMIN_ERRORS["ENABLED"]` is `False`.

**Too many issues, or the same error keeps creating new ones.** Check
`ADMIN_ERRORS["NEW_ISSUES_PER_MINUTE"]` (admission is capped per process) and
`ADMIN_ERRORS["MAX_ISSUES"]` (eviction). If unrelated errors are grouping into one issue, or one
error is fragmenting into many, pass an explicit `fingerprint=` to `capture_exception`/
`capture_message` to override the default grouping. `python manage.py errors_stats` prints row
counts, status breakdown and approximate on-disk size to help spot the pattern.

**The occurrence count grows but the detail page shows fewer events than that.** Expected: only
`ADMIN_ERRORS["EVENT_SAMPLE_PER_HOUR"]` full payloads are stored per fingerprint per hour, and only
the most recent `ADMIN_ERRORS["EVENTS_PER_ISSUE"]` are kept. A tight burst can also overflow the
writer's bounded queue (`QUEUE_MAXSIZE`), which drops the newest item rather than blocking the
request. `errors_stats` shows the writer's process-local drop counters.

**Is this safe from a PII / compliance standpoint?** Scrubbing is delegated entirely to Django's own
`get_exception_reporter_filter` — the same filter Django's technical 500 page and
`django.utils.log.AdminEmailHandler` use — never hand-rolled. `admin_errors.view_issue_context` is
the one permission that exposes headers, cookies, POST data, frame locals and Celery task args;
without it, a user only sees the traceback's file/line/function, not its variables or the request.
Settings and environment variables are never stored, unlike Django's debug page.

**Migrating from a `django-db-log`-style app.** There's no automated migration path: the schema and
fingerprinting are different, and there is deliberately no dependency on such a package. Run both in
parallel for a transition period, or accept that history starts fresh.

**`django.db.utils.OperationalError: database is locked` on SQLite.** See *SQLite tips* above (WAL
mode, `timeout`), or move `admin_errors` to its own database alias so its writes never contend with
the rest of the app.

## Non-goals and roadmap

Not aiming to replace Sentry (or similar) at scale: no alerting pipeline beyond the simple email
notifier, no release/deploy tracking, no cross-project or cross-environment search, no source map
support, no SDKs for other languages. If you outgrow this package, the event payload's shape (spec
section 6.4) is simple enough that a one-off export script is realistic.

## Contributing

All commands run from the repository root, non-interactively, with no virtualenv activated:

| What | Command |
|---|---|
| install | `uv sync --all-extras` |
| test | `uv run pytest -q` |
| test on PostgreSQL | `DJANGO_DB=postgres ADMIN_ERRORS_TEST_PG_URL=postgres://postgres:postgres@localhost:5432/admin_errors_test uv run pytest -q` |
| test on PostgreSQL (compose) | `make test-pg` — brings up `demo/docker-compose.yml`'s `postgres:16` service, runs the suite, always tears it down |
| bring up / down the PG container | `make pg-up` / `make pg-down` |
| lint | `uv run ruff check . && uv run ruff format --check . && uv run python -m django check --settings=tests.settings && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings` |
| full matrix | `uv run tox` |
| build | `uv run python -m build && uv run twine check dist/*` |
| e2e | `make e2e-up && uv run --extra e2e pytest e2e -q && make e2e-down` |
| demo (manual, blocks on `runserver`) | `make demo` (SQLite) · `make demo-pg` (Postgres via docker compose) |

See `docs/spec.md` for the full implementation specification, `docs/dev/adr/` for the accepted
design decisions, and `docs/user/` for operator-facing documentation (what an admin using the
*Errors* section of the site would read, as opposed to a contributor).

## Try it

`demo/` is a small, throwaway Django project (never published in the package) that exercises every
capture behaviour by hand, including the *Errors* section of the admin (issue list with sparklines,
detail page with traceback/request/occurrences, Resolve/Ignore/Reopen). The technical 500 page and
the console-logged exceptions are also worth poking at on their own.

```sh
make demo       # SQLite: migrate, seed 40 issues over 30 days, runserver 127.0.0.1:8000
make demo-pg    # same, against the docker-compose postgres:16 service (make pg-up/pg-down)
```

Both block on `runserver`; stop them with Ctrl-C. Log in at `/admin/` as `admin` / `admin` (created,
or its password reset, by `demo_seed` on every run). Then, from `/` (the index lists every link with
a one-line description):

| URL | What it shows |
|---|---|
| `/boom/` | `ZeroDivisionError`, unhandled → 500 |
| `/boom/<int:n>/` | `ValueError` with `n` in the message — message normalization: any `n` groups into the same issue |
| `/keyerror/<slug>/` | `KeyError` on `slug` — same grouping, any key |
| `/nested/` | a `RuntimeError` raised `from` an inner `ValueError` — chained exception stored |
| `/logged/` | caught, `logger.exception(...)`, still captured, returns 200 |
| `/warning/` | `logger.warning(...)` with `extra` — captured because the demo's `CAPTURE_LEVEL` is `"WARNING"` |
| `/sensitive/` | POST form with a password/token — payload is scrubbed, nothing sensitive stored |
| `/storm/?n=1000` | `n` occurrences of the same error in a loop — one issue, `count` grows by up to `n` (a tight burst can overflow the writer's bounded queue and drop some), only a handful of events stored (`EVENT_SAMPLE_PER_HOUR`) |
| `/unique-storm/?n=200` | `n` distinct fingerprints — new-issue creation capped by `NEW_ISSUES_PER_MINUTE` |
| `/task/` | a Celery task failure (eager without a broker, since the demo runs no worker) |
| `/async-boom/` | an `async def` view raising |
| `/404/` | `Http404` — confirms 404s are never captured |

Manual QA script (spec section 13): `make demo`, hit `/boom/` three times → one issue, count 3; hit
`/boom/1/` then `/boom/2/` → a second issue (`ValueError`, distinct from `/boom/`'s
`ZeroDivisionError`), count 2 despite the different `n`; hit `/storm/?n=5000` → response under 1 s,
one issue, at most `EVENT_SAMPLE_PER_HOUR` stored events, and the occurrence count grows by up to
5000 (a tight burst can overflow the writer's bounded queue and drop some occurrences — that counter
is process-local, so it is only visible in the same process, not through a separate `manage.py`
invocation); run `uv run python demo/manage.py errors_cleanup --dry-run` → a report of what retention
would delete.
A first hit of any issue also prints a "New issue: …" email to the console (the demo's
`EMAIL_BACKEND`), sent once per issue thanks to `NOTIFY_THROTTLE_SECONDS`. Resolving an issue in the
admin and triggering it again shows the *Regressed* badge on its detail page and, if that happens
after the throttle window has passed, a second "Regression: …" email; resolving and re-hitting inside
the window stays silent, since resolving does not reset the throttle.

## License

MIT, see `LICENSE`.
