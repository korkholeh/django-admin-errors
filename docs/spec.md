# django-admin-errors — Implementation Specification

Version 0.1 (implementation draft) · 2026-09-15
Language of code, docs, commit messages, and comments: English.

---

## 0. How to read this document

This spec is written for autonomous, phase-by-phase implementation by Claude Code (Section 15).
Rules for the implementer:

- Every phase lists deliverables and **acceptance criteria**. A phase is done only when all of its
  criteria pass on SQLite and (when a Postgres URL is available) on PostgreSQL.
- Wherever a design choice is open, the value marked **Default** is the decision. Do not ask; take it.
- Section 2 lists non-goals. They are out of scope even when they look cheap to add.
- Section 16 lists known gotchas. Read it before starting Phase 2.
- Keep the package free of runtime dependencies other than Django. Celery, pytest, ruff, etc. are
  optional/dev extras only.

---

## 1. Summary

`django-admin-errors` (import name `admin_errors`) is a reusable Django app that records unhandled
exceptions and error-level log records into the project's own database and shows them inside the
Django admin, Sentry-style: grouped into issues, deduplicated, with traceback, request context,
counts and 14/30-day trends.

Target users: Django projects where Sentry is overkill or not allowed (data residency, air-gapped,
small internal tools), and where the operator already lives in the Django admin.

Design pillars:

1. **Zero runtime dependencies** beyond Django. First-class SQLite and PostgreSQL.
2. **Never slows the request path, never raises.** Capture is cheap and synchronous; DB writes happen
   in a background thread with its own DB connection.
3. **Bounded storage.** Dedup by fingerprint, sampling of full payloads, hard caps, retention,
   SQLite vacuum support.
4. **Native admin UI.** Django admin templates, admin CSS variables (dark mode works), model
   permissions, admin actions. No JS framework, no external assets.
5. **Safe by default.** Reuses Django's `SafeExceptionReporterFilter` so passwords, tokens, cookies and
   `@sensitive_variables` are scrubbed exactly as on the DEBUG 500 page.

Historical note for motivation: Sentry itself started as `django-db-log`, a Django app writing to the
project DB. This library is the modern, bounded version of that idea.

---

## 2. Goals and non-goals

### Goals (v0.1)

- Capture: unhandled exceptions in views (sync + ASGI), `logger.error/exception` anywhere, Celery
  task failures, management commands, plus a manual `capture_exception()` / `capture_message()` API.
- Group into issues by fingerprint; count every occurrence; store full payload for a sample of them.
- Retention and caps that keep DB size bounded without operator attention.
- Admin UI: issue list with counts/trends/filters/search, issue detail with traceback, request
  context, recent events, daily chart; resolve/ignore/reopen; regression detection.
- Permissions: standard model perms plus a separate permission for sensitive context.
- Email notification on new issue / regression, throttled. Signals for custom notifiers.
- Test suite on SQLite and PostgreSQL across supported Django/Python versions.
- Demo project that shows everything and seeds realistic data for screenshots.

### Non-goals (v0.1)

Performance monitoring / tracing, breadcrumbs, releases and deploy tracking, frontend/JS error intake,
source maps, alert-rule engine, multi-project/tenant separation, HTTP ingest API, user feedback,
Slack/Telegram notifiers (third parties hook the signals), MySQL/MariaDB support (must not break on
import, but untested), Django REST API.

---

## 3. Compatibility and constraints

| Item | Requirement |
|---|---|
| Python | 3.10 – 3.13 |
| Django | 4.2 LTS through the current stable release. Verify the current list on djangoproject.com at implementation time and include every supported version in the matrix. |
| Databases | SQLite ≥ 3.9 (JSON1); PostgreSQL ≥ 13. Tests run on both. |
| Deployment | WSGI (gunicorn incl. `--preload`, uwsgi), ASGI (uvicorn/daphne), `runserver`. |
| Optional integrations | Celery ≥ 5 (signals only, imported guardedly). |
| Runtime deps | Django only. |
| Packaging | `src/` layout, `pyproject.toml` (hatchling), MIT license, `django-admin-errors` on PyPI (if the name is taken, fall back to `django-admin-errorlog`; import name stays `admin_errors`). |
| Code style | ruff (lint + format), line length 100, type hints on all public functions. mypy is optional locally, not a CI gate. |
| Minimal host | Must install, migrate and run with only `django.contrib.{admin,auth,contenttypes,sessions,messages}` present. |

---

## 4. Package layout

```
django-admin-errors/
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── LICENSE
├── Makefile
├── tox.ini
├── .github/workflows/ci.yml
├── src/admin_errors/
│   ├── __init__.py            # __version__ only (keep import side-effect free)
│   ├── apps.py                # AppConfig.ready(): install logging handler, connect signals, checks
│   ├── conf.py                # settings proxy with defaults, reloads on setting_changed
│   ├── checks.py              # django system checks
│   ├── models.py
│   ├── migrations/
│   ├── api.py                 # public API: capture_exception, capture_message, flush
│   ├── capture.py             # builds CapturedItem from exc_info / log record; ignore rules; sampling
│   ├── context.py             # request/user/celery context extraction, scrubbing, truncation
│   ├── fingerprint.py
│   ├── handlers.py            # logging.Handler
│   ├── middleware.py          # RequestContextMiddleware (contextvar with current request)
│   ├── writer.py              # background thread, queue, aggregation, flush scheduling
│   ├── storage.py             # store_batch(): all ORM writes for the capture path
│   ├── retention.py           # cleanup rules, eviction, sqlite vacuum
│   ├── signals.py
│   ├── notifications.py       # EmailNotifier
│   ├── routers.py             # optional DB router for a dedicated alias
│   ├── integrations/
│   │   ├── __init__.py
│   │   └── celery.py          # task_failure receiver, registered only if celery is importable
│   ├── tasks.py               # celery shared_task for cleanup (guarded import)
│   ├── admin.py               # IssueAdmin, custom views, actions, register(site)
│   ├── templatetags/admin_errors_tags.py   # sparkline, level/status badges, humanize
│   ├── templates/admin/admin_errors/issue/
│   │   ├── change_list.html
│   │   ├── change_form.html   # issue detail
│   │   ├── event_detail.html
│   │   └── includes/{traceback,request,events,chart,cards}.html
│   ├── static/admin_errors/{admin_errors.css,admin_errors.js}
│   └── management/commands/
│       ├── errors_cleanup.py
│       ├── errors_test.py     # emits one test error through the full pipeline
│       └── errors_stats.py    # row counts, oldest/newest, approx size per table
├── tests/                     # pytest, see Section 14
├── benchmarks/bench_capture.py
└── demo/                      # see Section 13
```

---

## 5. Settings

All settings live in one dict `ADMIN_ERRORS` in the host project's `settings.py`. `admin_errors.conf.settings`
exposes attributes with defaults; unknown keys raise a system-check warning (`admin_errors.W001`).

| Key | Default | Meaning |
|---|---|---|
| `ENABLED` | `True` | Master switch. When `False`, capture is a no-op; admin still works. |
| `DATABASE` | `"default"` | DB alias used for all reads/writes. See 11.3 for a dedicated alias. |
| `TRANSPORT` | `"thread"` | `"thread"` (background writer) or `"sync"` (write inline; used in tests and for debugging). |
| `CAPTURE_LEVEL` | `"ERROR"` | Minimum log level captured by the logging handler. |
| `AUTO_INSTALL_LOGGING_HANDLER` | `True` | In `ready()`, attach `AdminErrorsHandler` to the root logger if not already attached. |
| `CAPTURE_IN_DEBUG` | `True` | Capture when `settings.DEBUG` is `True`. |
| `IGNORE_LOGGERS` | `["django.security.DisallowedHost"]` | Logger names (exact or prefix with trailing `.`) never captured. |
| `IGNORE_EXCEPTIONS` | `["django.http.Http404", "django.core.exceptions.PermissionDenied"]` | Dotted paths; subclasses also ignored. |
| `IGNORE_HTTP_STATUS_BELOW` | `500` | Records from `django.request` / `django.server` carrying `status_code` below this are dropped regardless of level. |
| `BEFORE_SEND` | `None` | Dotted path to `callable(payload: dict, hint: dict) -> dict | None`. Return `None` to drop. Runs in the capturing thread. |
| `IN_APP_INCLUDE` | `None` | List of path prefixes considered in-app. Default: `settings.BASE_DIR` if defined. |
| `IN_APP_EXCLUDE` | `["site-packages", "dist-packages", "/lib/python"]` | Substrings marking non-app frames. |
| `CAPTURE_LOCALS` | `True` | Store local variables for sampled events (scrubbed). |
| `CAPTURE_REQUEST_BODY` | `False` | Store raw request body (truncated to `MAX_BODY_BYTES`). |
| `MAX_BODY_BYTES` | `4096` | |
| `MAX_VAR_REPR_LENGTH` | `200` | Truncate every variable repr. |
| `MAX_FRAMES` | `50` | Keep innermost N frames (in-app frames prioritised are not cut before library frames — simply keep the last N). |
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
| `MAX_ISSUES` | `5000` | Hard cap; eviction order in 9.2. |
| `CLEANUP` | `"opportunistic"` | `"opportunistic"` (writer thread runs cleanup ≤ once per `CLEANUP_INTERVAL_SECONDS`), or `"off"` (only command / Celery task). |
| `CLEANUP_INTERVAL_SECONDS` | `3600` | |
| `SQLITE_VACUUM` | `"incremental"` | `"incremental"` runs `PRAGMA incremental_vacuum` after cleanup when `auto_vacuum=2`; `"off"` never. Full `VACUUM` only via `errors_cleanup --vacuum`. |
| `NOTIFY_BACKEND` | `"admin_errors.notifications.EmailNotifier"` | Dotted path or `None`. |
| `NOTIFY_RECIPIENTS` | `None` | `None` → `settings.ADMINS` emails. |
| `NOTIFY_THROTTLE_SECONDS` | `3600` | Minimum gap between notifications for the same issue. |
| `NOTIFY_ON` | `["created", "regressed"]` | Subset of `created`, `regressed`. |
| `ADMIN_SITE` | `None` | Dotted path to an `AdminSite` instance to register with. `None` → `django.contrib.admin.site`. Set to `False` to skip auto-registration and call `admin_errors.admin.register(site)` manually. |
| `INTERNAL_LOGGING` | `False` | Log internal problems (dropped items, DB failures) to logger `admin_errors.internal` at WARNING. Off by default to avoid noise. |

`conf.settings` re-reads the dict on Django's `setting_changed` signal so `override_settings` works in tests.

---

## 6. Data model

App label: `admin_errors`. Verbose app name in admin: **Errors**. All timestamps are timezone-aware UTC.
Daily buckets use the UTC date of the event timestamp; the admin labels charts "per day (UTC)".

### 6.1 `Issue`

| Field | Type | Notes |
|---|---|---|
| `fingerprint` | `CharField(40, unique=True)` | sha1 hex |
| `exception_type` | `CharField(200, db_index=True)` | e.g. `ValueError`; for message-only records the logger name |
| `title` | `CharField(500)` | normalized message, first line, truncated |
| `culprit` | `CharField(500, blank=True)` | `package.module.function` of the innermost in-app frame |
| `level` | `CharField(10, choices=critical/error/warning/info)` | from log level; exceptions from views are `error` |
| `status` | `CharField(10, choices=open/resolved/ignored, default open, db_index)` | |
| `first_seen` | `DateTimeField` | |
| `last_seen` | `DateTimeField(db_index=True)` | |
| `count` | `PositiveBigIntegerField(default=0)` | total occurrences, including unsampled |
| `resolved_at` | `DateTimeField(null=True)` | kept after reopen to render a "regressed" badge |
| `resolved_by` | `FK(AUTH_USER_MODEL, null, SET_NULL)` | |
| `notified_at` | `DateTimeField(null=True)` | notification throttle |
| `last_event` | `JSONField(null=True)` | payload of the most recently *stored* event, so the detail page always renders even after event cleanup |

`Meta`: `ordering = ["-last_seen"]`, `indexes = [Index(fields=["status", "-last_seen"])]`,
`permissions = [("view_issue_context", "Can view request context and local variables")]`.

### 6.2 `Event`

| Field | Type |
|---|---|
| `issue` | `FK(Issue, CASCADE, related_name="events")` |
| `timestamp` | `DateTimeField(db_index=True)` |
| `payload` | `JSONField` (Section 6.4) |

`Meta`: `indexes = [Index(fields=["issue", "-timestamp"])]`.

### 6.3 `IssueDailyCount`

| Field | Type |
|---|---|
| `issue` | `FK(Issue, CASCADE, related_name="daily_counts")` |
| `date` | `DateField` |
| `count` | `PositiveIntegerField(default=0)` |

`Meta`: `constraints = [UniqueConstraint(fields=["issue", "date"])]`, `indexes = [Index(fields=["date"])]`.

### 6.4 Event payload (JSON, schema version 1)

```json
{
  "v": 1,
  "timestamp": "2026-09-15T10:22:31.412Z",
  "level": "error",
  "logger": "django.request",
  "message": "Internal Server Error: /checkout/",
  "source": "exception",                      // "exception" | "message"
  "exception": {
    "type": "ValueError", "module": "builtins", "value": "invalid literal for int()",
    "chain": [                                  // outermost first; each has its own frames
      {"type": "...", "value": "...", "cause": true, "frames": [ ...frame... ]}
    ]
  },
  "frames": [                                   // frames of the outermost exception, innermost last
    {"filename": "/app/shop/views.py", "lineno": 88, "function": "checkout", "module": "shop.views",
     "in_app": true, "pre_context": ["..."], "context_line": "...", "post_context": ["..."],
     "vars": {"order_id": "42", "password": "********"}}
  ],
  "request": {
    "method": "POST", "url": "https://example.com/checkout/?step=2", "path": "/checkout/",
    "query": {"step": "2"}, "post": {"card": "********"},
    "headers": {"Content-Type": "...", "Authorization": "********"},
    "cookies": {"sessionid": "********"},
    "remote_addr": "203.0.113.4", "body": null,
    "user": {"id": 7, "username": "oleh", "email": "..."}
  },
  "celery": {"task": "shop.tasks.send_receipt", "task_id": "…", "args": "[42]", "kwargs": "{}"},
  "extra": {},                                  // from LogRecord.extra / capture_* kwargs
  "server": {"hostname": "web-1", "pid": 1234, "python": "3.12.3", "django": "5.2.1"}
}
```

Rules: every value is JSON-serialisable (strings for reprs). `request`, `celery` and `extra` are
optional. `frames[*].vars` is omitted when `CAPTURE_LOCALS=False` or the viewer lacks permission
(gating is at render time; storage always keeps scrubbed vars when enabled).

---

## 7. Capture pipeline

### 7.1 Entry points

1. **Logging handler** `admin_errors.handlers.AdminErrorsHandler(logging.Handler)` — the universal
   entry. Installed on the root logger by `ready()` (see `AUTO_INSTALL_LOGGING_HANDLER`) or by the
   project's `LOGGING` dict. Django's own `django.request` logger already logs 5xx with
   `exc_info` and `extra={"status_code": 500, "request": request}`, so unhandled view exceptions are
   captured through this handler without any middleware.
2. **`got_request_exception` signal** — receiver exists only as a fallback to attach the request to
   the exception object (`exc.__admin_errors_request__ = request`) when the project's logging
   config prevents the `django.request` record from reaching the root logger. The receiver never
   captures by itself; it marks, the handler captures. (Default: enabled.)
3. **`RequestContextMiddleware`** (optional, recommended) — stores the current request in a
   `contextvars.ContextVar` so `logger.exception()` inside a view gets request context. Works for
   sync and async (`sync_capable = async_capable = True`). asgiref propagates contextvars through
   `sync_to_async`, so thread-pool execution of sync views keeps the context.
4. **Celery** — `admin_errors.integrations.celery` connects `task_failure` when Celery is importable
   and adds `celery` context. See 11.1 for double-capture prevention.
5. **Manual API** `admin_errors.api`:
   - `capture_exception(exc: BaseException | None = None, *, request=None, extra=None, fingerprint=None, level="error") -> str | None` (returns fingerprint or `None` if dropped). `exc=None` → `sys.exc_info()`.
   - `capture_message(message: str, *, level="error", request=None, extra=None, fingerprint=None) -> str | None`.
   - `flush(timeout: float | None = 2.0) -> None` — block until the writer queue is drained (used in tests, `atexit`, and management commands).

### 7.2 Steps executed in the capturing thread (synchronously)

Cost budget: ≤ 2 ms p50 without locals, ≤ 10 ms with locals on a 30-frame traceback. Cheap checks
come first so the common "drop" paths are near-free.

1. **Guards**: `ENABLED`; `DEBUG` vs `CAPTURE_IN_DEBUG`; recursion guard (thread-local flag set while
   inside the pipeline, and permanently set for the writer thread); logger in `IGNORE_LOGGERS`;
   `status_code` rule; exception type in `IGNORE_EXCEPTIONS` (incl. subclasses).
2. **Once-per-exception guard**: if the exception object has attribute `__admin_errors_captured__`,
   drop. Otherwise set it. This prevents double capture when middleware logs and re-raises, or when
   Celery logs the failure and also fires `task_failure`.
3. **Fingerprint** (Section 8). Explicit `fingerprint` kwarg / `record.fingerprint` wins.
4. **New-fingerprint admission**: process-local bounded LRU set (10k) of seen fingerprints + token
   bucket `NEW_ISSUES_PER_MINUTE`. Unseen fingerprint with empty bucket → drop, increment internal
   `dropped_new_issue` counter.
5. **Sampling decision**: per-fingerprint token bucket `EVENT_SAMPLE_PER_HOUR` (process-local, bounded
   LRU 10k). If a token is available → build the full payload. Otherwise build a **count-only
   item**: fingerprint + meta only (type, title, culprit, level, logger). Meta is always cheap to
   build, so an issue can be created even from a count-only item (needed if retention deleted the
   issue while the process bucket is exhausted).
6. **Payload build** (`context.py`) for sampled items:
   - Frames via `django.views.debug.ExceptionReporter(request, exc_type, exc_value, tb).get_traceback_frames()`.
     This already applies `get_exception_reporter_filter(request)` (respects
     `DEFAULT_EXCEPTION_REPORTER_FILTER` and `@sensitive_variables`) and handles chained exceptions.
     Do **not** call `get_traceback_data()` (it also collects settings and is slower).
   - Each var value is turned into a string with a safe `repr()` (catch everything; fall back to
     `<unrepresentable ClassName>`), truncated to `MAX_VAR_REPR_LENGTH`.
   - `module` per frame from `tb_frame.f_globals.get("__name__")`; `in_app` per 5.
   - Request: `filter.get_safe_request_meta(request)` → headers (only `HTTP_*`, `CONTENT_TYPE`,
     `CONTENT_LENGTH`, converted to header-case), `filter.get_post_parameters(request)` (respects
     `@sensitive_post_parameters`), `filter.get_safe_cookies(request)`, `request.GET`,
     `request.build_absolute_uri()` (guarded: `DisallowedHost` → path only), user
     (`request.user` if authenticated; wrapped in try/except — user may not be loaded), remote addr
     from `REMOTE_ADDR`. Body only if `CAPTURE_REQUEST_BODY`.
   - `extra`: `LogRecord` attributes not in the standard set (same approach as `python-json-logger`),
     values coerced with the same safe repr.
   - `BEFORE_SEND` hook, then size enforcement (`MAX_PAYLOAD_BYTES`, degrade in the documented order).
7. **Enqueue** `CapturedItem(fingerprint, meta, timestamp, payload | None)` to the writer
   (`TRANSPORT="thread"`) or call `storage.store_batch({fp: agg})` directly (`"sync"`).

Nothing in steps 1–7 may raise. Wrap the whole pipeline in `try/except BaseException` (excluding
`KeyboardInterrupt`/`SystemExit`, which are re-raised) and, when `INTERNAL_LOGGING` is on, log once
per exception type per process.

---

## 8. Fingerprinting

Goal: the same bug from different call sites and with varying data collapses into one issue; different
bugs never collapse. Output: `sha1(":".join(parts)).hexdigest()`.

### 8.1 Exceptions

Parts, in order:

1. `exception_type` — fully qualified type of the **outermost** exception (`module.QualName`).
2. `culprit` — `module.function` of the innermost **in-app** frame of the outermost exception's
   traceback; if no in-app frame exists, the innermost frame overall.
3. `normalized_message` — `str(exc)` (first line, max 200 chars) after normalization:
   - UUIDs → `<uuid>`; hex strings ≥ 8 chars → `<hex>`; `0x…` addresses → `<addr>`;
   - ISO-8601 dates/times → `<ts>`; any remaining digit run → `#`;
   - single- and double-quoted string literals → `<str>` (turns `KeyError: 'foo'` and
     `KeyError: 'bar'` into one issue — accepted trade-off; users can override with `fingerprint=`);
   - collapse whitespace.

`culprit` in the parts already differentiates identical messages raised from different functions.

### 8.2 Message-only records (no `exc_info`)

Parts: `logger name`, `record.levelname`, `record.msg` (the **unformatted** template, e.g.
`"Payment failed for order %s"`), not `record.getMessage()`. The template is the natural grouping key
and needs no normalization. If `record.msg` is not a string, use its normalized `str()`.

### 8.3 Overrides

- `logger.error("...", extra={"fingerprint": "payments-timeout"})` or `capture_*(fingerprint=...)`
  uses the given value verbatim (still hashed).
- `title` for the issue is the formatted message (first line, ≤ 500 chars) of the first stored event;
  `exception_type`/`culprit` come from the fingerprint inputs.

Tests must pin the algorithm: given fixed inputs the hash must be stable across releases; changing
it is a breaking change (issues would duplicate).

---

## 9. Writer, storage and retention

### 9.1 Writer thread (`writer.py`)

- Singleton `Writer` with `queue.Queue(maxsize=QUEUE_MAXSIZE)`, a daemon thread, and a `sink`
  callable (default `storage.store_batch`). The sink abstraction makes thread mechanics testable
  without a DB.
- **Lazy start on first `enqueue()`**; store `os.getpid()` at start. On every `enqueue()` compare the
  pid — if it differs (gunicorn `--preload`, uwsgi, multiprocessing) discard the inherited state and
  start a fresh thread in the child. Never start a thread at import or in `ready()`.
- The writer thread sets the recursion-guard flag for its whole lifetime.
- **Loop**: `queue.get(timeout=FLUSH_INTERVAL_SECONDS)`; aggregate into
  `dict[fingerprint, Aggregate(meta, count, first_ts, last_ts, samples: list[(ts, payload)])]`.
  Flush when the interval elapsed since the first item of the batch, when `FLUSH_BATCH_SIZE` is
  reached, or when `flush()` is requested. Count-only items only increment `count`.
- **Overflow**: `put_nowait` failure → drop the item and increment `stats.dropped_queue_full`.
- **Connection hygiene**: `close_old_connections()` before each flush; after a flush, `connections[alias].close()`
  if the thread has been idle for more than 60 s (do not hold an idle connection open forever), and
  unconditionally once the writer thread stops, so no connection outlives the thread that opened it.
- **Failure handling**: any exception in `sink` is swallowed. `OperationalError` (e.g. SQLite
  `database is locked`) → one retry after 100 ms, then drop the batch. Never re-enqueue.
- **Shutdown**: `atexit` → `flush(timeout=2.0)`. `flush()` uses a `threading.Event` per request and
  returns after the timeout even if the queue is not empty.
- `Writer.stats` (dropped counters, flushes, last error type) is exposed via `errors_stats`.
- `TRANSPORT="sync"` bypasses the thread entirely and calls the sink inline. Use it in tests and
  when debugging.

### 9.2 `storage.store_batch(batch, *, using)`

One short `transaction.atomic(using=alias)` per aggregate (not per batch, so one bad row does not
lose the batch). Per aggregate:

1. `Issue.objects.using(alias).filter(fingerprint=fp).values("id", "status", "notified_at", "resolved_at").first()`.
2. If missing: create inside a nested `atomic()` (savepoint — required on PostgreSQL so an
   `IntegrityError` does not poison the outer transaction); on `IntegrityError` re-run step 1 (race
   with another process). Creation sets `first_seen=first_ts`, `title/culprit/type/level` from meta,
   `count=0`, `last_event` from the first sample if any. Fire `issue_created` (Section 10) after the
   transaction commits (`transaction.on_commit`).
3. `UPDATE … SET count = count + n, last_seen = GREATEST(last_seen, last_ts)` via `F()`; on SQLite
   use `Case/When` or two-step (`last_seen` is monotonically increasing in practice; a plain
   assignment is acceptable if `last_ts >= existing`, so just assign when newer).
4. Status handling: `resolved` → set `open`, keep `resolved_at`, fire `issue_regressed`.
   `ignored` → counters only; skip steps 5–6.
5. Samples: bulk-insert `Event` rows (`payload` from samples), set `Issue.last_event` to the newest.
   Then trim the ring buffer:
   `ids = Event.objects.filter(issue_id=id).order_by("-timestamp", "-id").values_list("id", flat=True)[EVENTS_PER_ISSUE:]`,
   delete those ids (list first, then `filter(id__in=…).delete()`).
6. Daily counts: for each UTC date in the aggregate (normally one, at most two around midnight):
   `UPDATE … count = count + n WHERE issue_id AND date`; if 0 rows, create in a savepoint;
   on `IntegrityError` retry the update.

Query budget for one aggregate with `k` samples: 1 SELECT + 1 UPDATE (+1 INSERT for a new issue)
+ 1 bulk INSERT + 1 SELECT/DELETE for trimming + 1 UPDATE (or INSERT) daily. Tests assert with
`assertNumQueries` (upper bound, both backends).

### 9.3 Retention (`retention.run_cleanup(*, using, vacuum=False, dry_run=False) -> CleanupReport`)

Run in this order; every delete is done in id chunks of 1000 to keep SQLite write locks short:

1. `Event` older than `EVENT_RETENTION_DAYS`.
2. `IssueDailyCount` older than `DAILY_COUNT_RETENTION_DAYS`.
3. `Issue` with `status=resolved` and `max(resolved_at, last_seen) < now - RESOLVED_ISSUE_TTL_DAYS`.
4. `Issue` with `status=ignored` and `last_seen < now - IGNORED_ISSUE_TTL_DAYS` (skipped when `None`).
5. `Issue` with `status=open` and `last_seen < now - OPEN_ISSUE_TTL_DAYS`.
6. Eviction: while `Issue.count() > MAX_ISSUES`, delete the oldest by `last_seen` in the order
   `ignored` → `resolved` → `open`, until the count is `≤ int(MAX_ISSUES * 0.9)` (hysteresis).
7. SQLite only: if `SQLITE_VACUUM="incremental"` and `PRAGMA auto_vacuum` returns `2`, run
   `PRAGMA incremental_vacuum`. If `vacuum=True`, run full `VACUUM` (outside any transaction; the
   command warns that it rewrites the file and locks the DB).

`CleanupReport` returns counts per step; the command prints them, `--dry-run` only counts.

Triggers:

- `manage.py errors_cleanup [--vacuum] [--dry-run] [--database ALIAS]`.
- Celery: `admin_errors.tasks.cleanup` (`shared_task`, defined only if Celery is importable). The
  README shows a beat schedule.
- Opportunistic (`CLEANUP="opportunistic"`): after a flush the writer thread checks a process-local
  timestamp (never more than once per `CLEANUP_INTERVAL_SECONDS` per process) **and**
  `cache.add("admin_errors:cleanup-lock", 1, CLEANUP_INTERVAL_SECONDS)` to avoid all processes
  cleaning at once. With `DummyCache`, `add()` always succeeds, so the process-local timestamp is the
  only limiter — acceptable.

### 9.4 Storage bound (document in README)

`Issues ≤ MAX_ISSUES`; `Events ≤ MAX_ISSUES × EVENTS_PER_ISSUE` with payload ≤ `MAX_PAYLOAD_BYTES`
→ worst case ≈ 5000 × 20 × 64 KB ≈ 6.4 GB theoretical, ≈ tens of MB in practice because most issues
have 1–3 events and payloads are 5–15 KB. `DailyCounts ≤ MAX_ISSUES × DAILY_COUNT_RETENTION_DAYS`
rows of ~40 bytes. `errors_stats` prints actual numbers.

---

## 10. Signals and notifications

`admin_errors.signals`:

| Signal | Arguments | Fired |
|---|---|---|
| `issue_created` | `issue`, `event` (may be `None`) | after commit, first time a fingerprint is stored |
| `issue_regressed` | `issue`, `event` | after commit, when a resolved issue receives a new occurrence |
| `issue_status_changed` | `issue`, `old_status`, `new_status`, `user` | from admin actions/views |

Receivers of the first two run **in the writer thread** (or inline with `TRANSPORT="sync"`).
Exceptions in receivers are swallowed and counted. Receivers must not block for long.

`EmailNotifier`:

- Connects to `issue_created` / `issue_regressed` per `NOTIFY_ON`.
- Throttle: send only if `issue.notified_at` is `None` or older than `NOTIFY_THROTTLE_SECONDS`;
  update `notified_at` with an `UPDATE … WHERE notified_at IS NULL OR notified_at < threshold`
  so concurrent processes do not double-send.
- Uses `django.core.mail.send_mail` to `NOTIFY_RECIPIENTS` or `settings.ADMINS`, subject
  `[<SITE or hostname>] New issue: ValueError in shop.views.checkout` /
  `… Regression: …`; plain-text body with title, culprit, count, first/last seen, a short traceback
  (frames only, no vars), and the admin URL (`reverse("admin:admin_errors_issue_change")`,
  prefixed with `NOTIFY_BASE_URL` setting — add this key, default `""`).
- README documents replacing Django's `mail_admins` handler (which emails on every occurrence) with
  this notifier.

---

## 11. Integrations and deployment

### 11.1 Celery

`integrations/celery.py` connects `task_failure(sender, task_id, exception, args, kwargs, traceback, einfo)`
and calls the pipeline with `celery={"task": sender.name, "task_id": task_id, "args": repr(args)[:500], "kwargs": repr(kwargs)[:500]}`.
Celery also logs the failure through `celery.app.trace` at ERROR with `exc_info`; the once-per-exception
guard (7.2 step 2) makes whichever path runs first win. Enabled automatically when `celery` imports;
no Celery-specific settings.

### 11.2 Logging configuration

`AUTO_INSTALL_LOGGING_HANDLER=True` attaches the handler to the root logger at `CAPTURE_LEVEL`.
System check `admin_errors.W002` warns when the project's `LOGGING` sets `propagate: False` on
`django.request` or `django` without listing `admin_errors` among that logger's handlers, because
then unhandled view exceptions would not be captured. README shows the explicit `LOGGING` form:

```python
"handlers": {"admin_errors": {"class": "admin_errors.handlers.AdminErrorsHandler", "level": "ERROR"}},
"root": {"handlers": ["console", "admin_errors"], "level": "WARNING"},
```

### 11.3 Dedicated database alias

`ADMIN_ERRORS["DATABASE"] = "errors"` plus a `DATABASES["errors"]` entry (typically a separate SQLite
file next to a Postgres main DB, or the same Postgres server with its own database) and
`DATABASE_ROUTERS = ["admin_errors.routers.AdminErrorsRouter"]`. The router sends all `admin_errors`
models to that alias and `allow_migrate` keeps them out of other aliases. System check
`admin_errors.E001` errors when the alias does not exist. Benefits: no growth of the main DB, no
write-lock contention on SQLite projects, trivial full `VACUUM`. Recommended in README for SQLite
projects.

### 11.4 ASGI / async

Handler and API are thread-agnostic. `RequestContextMiddleware` uses a `ContextVar`; asgiref
copies context into `sync_to_async` threads, so sync views under ASGI keep the request context.
Tests include an async view raising inside `AsyncClient`.

### 11.5 System checks (`checks.py`)

`W001` unknown setting key · `W002` logging propagation (11.2) · `E001` missing DB alias ·
`W003` `mail_admins` handler still active while `EmailNotifier` is enabled (informational) ·
`E002` SQLite < 3.9 (no JSON1).

---

## 12. Admin UI

### 12.1 Registration

`admin.py` defines `IssueAdmin` and `register(site: AdminSite) -> None`. On import, if
`ADMIN_SITE` is not `False`, register with the resolved site. `Event` and `IssueDailyCount` are not
registered as separate models; events are reached through the issue.

### 12.2 Issue list (`change_list.html` extends `admin/change_list.html`)

- Summary cards above the results (only when the user has `view_issue`): **Unresolved issues**,
  **Events last 24 h** (sum of `IssueDailyCount` for today+yesterday UTC), **New issues last 24 h**.
  Three aggregate queries.
- Columns: *Issue* (exception type in bold, title, culprit in muted small text; the whole cell links
  to the detail view), *Count*, *Last seen* (`timesince` + full timestamp in `title`), *First seen*,
  *Status* badge (`open` / `resolved` / `ignored`; `open` with `resolved_at` set renders
  **regressed**), *Trend* (14-day sparkline, inline SVG 120×24 built by a template tag from prefetched
  daily counts — one extra query for the page via `Prefetch` filtered to the last 14 days; no N+1,
  asserted in tests).
- `list_filter`: status, level, exception_type, custom `LastSeenFilter` (1 h / 24 h / 7 d / 30 d).
- `search_fields`: `title`, `exception_type`, `culprit`.
- `ordering = ["-last_seen"]`, `list_per_page = 50`, `date_hierarchy` not used.
- Actions: **Resolve**, **Ignore**, **Reopen** (require `change_issue`), **Delete selected** (default
  admin action, requires `delete_issue`). Each fires `issue_status_changed`.
- `has_add_permission` → `False`. `has_change_permission` returns `True` only for the status
  actions; the form is fully read-only (`readonly_fields = all`).

### 12.3 Issue detail (`change_form.html` extends `admin/change_form.html`)

Rendered from `issue.last_event` by default, or from a selected event
(`…/<pk>/events/<event_id>/`, `event_detail.html`). Sections:

1. **Header**: type, title, culprit, badges (level, status, regressed), count, first/last seen,
   buttons *Resolve* / *Ignore* / *Reopen* / *Delete* (POST to `…/<pk>/status/<action>/`, wrapped in
   `site.admin_view`, CSRF-protected, permission-checked, then `messages.success` + redirect).
2. **Traceback**: frames innermost last, in-app frames highlighted, library frames collapsed by
   default, context lines with the failing line marked, per-frame *Locals* toggle (rendered only
   with `view_issue_context`). Chained exceptions rendered as separate blocks with
   "The above exception was the direct cause…" separators. A **Copy as text** button copies a plain
   Python-style traceback (built server-side, in a hidden `<pre>`).
3. **Request** (`view_issue_context` required beyond method + path): URL, GET, POST, headers,
   cookies, remote addr, user.
4. **Celery** and **Extra** when present.
5. **Occurrences**: 30-day bar chart (inline SVG, one query) and the list of stored events
   (timestamp, path or task, user) linking to the event detail.
6. **Server**: hostname, pid, Python/Django versions.

### 12.4 Styling and assets

- Only Django admin CSS variables (`--primary`, `--body-bg`, `--body-fg`, `--darkened-bg`,
  `--hairline-color`, `--error-fg`, `--message-warning-bg`, etc.). Must look correct in light and dark
  themes (verify visually in the demo with the browser theme toggled).
- `admin_errors.css` ≤ 6 KB; `admin_errors.js` ≤ 3 KB vanilla (toggles, copy button). No CDN, no
  inline scripts (CSP-friendly).
- All templates use `{% load i18n %}` and `{% translate %}`; ship `locale/uk/LC_MESSAGES` with a
  Ukrainian translation of the UI strings (Phase 5).

### 12.5 Permissions

| Permission | Grants |
|---|---|
| `admin_errors.view_issue` | list, detail without sensitive context, traceback without locals |
| `admin_errors.view_issue_context` | request headers/cookies/GET/POST/user, Celery args, frame locals, `extra` |
| `admin_errors.change_issue` | resolve / ignore / reopen (actions and buttons) |
| `admin_errors.delete_issue` | delete issues (cascade deletes events and daily counts) |
| `admin_errors.add_issue` | unused; `has_add_permission` is always `False` |

Superusers pass all checks. Gating is enforced in views and templates (`perms.admin_errors.view_issue_context`),
and covered by tests for each permission in isolation.

---

## 13. Demo project (`demo/`)

Purpose: manual QA, screenshots for the README, and a reproducible environment for both backends.

```
demo/
├── manage.py
├── docker-compose.yml          # postgres:16 service
├── demo_project/settings.py    # sqlite default; DEMO_DB=postgres switches to the compose DB
├── demo_project/urls.py        # admin + demo_app urls
├── demo_project/celery.py      # optional; CELERY_TASK_ALWAYS_EAGER=True by default
└── demo_app/
    ├── views.py
    ├── tasks.py
    └── management/commands/demo_seed.py
```

Settings: `ADMIN_ERRORS` with `TRANSPORT="thread"`, `CAPTURE_LEVEL="WARNING"`,
`EVENT_SAMPLE_PER_HOUR=5`, small retention values so cleanup is observable
(`EVENT_RETENTION_DAYS=7`, `MAX_ISSUES=200`), `INTERNAL_LOGGING=True`, `ADMINS` set and
`EMAIL_BACKEND` = console. `RequestContextMiddleware` enabled.

`demo_app/views.py` URL map:

| URL | Behaviour |
|---|---|
| `/` | index page listing all demo links with a one-line description each |
| `/boom/` | raises `ZeroDivisionError` in the view (unhandled → 500) |
| `/boom/<int:n>/` | raises `ValueError(f"bad value {n}")` — demonstrates message normalization (one issue for any `n`) |
| `/keyerror/<slug>/` | raises `KeyError(slug)` — one issue regardless of key |
| `/nested/` | raises `RuntimeError` from an inner `except` (chained exception) |
| `/logged/` | catches an exception, `logger.exception(...)`, returns 200 |
| `/warning/` | `logger.warning("Slow payment provider %s", name)` with `extra` |
| `/sensitive/` | POST form with `password` field, `@sensitive_variables("token")`, `Authorization` header — raises; demonstrates scrubbing |
| `/storm/?n=1000` | captures `n` occurrences of the same error via `capture_exception` in a loop, returns elapsed time — demonstrates aggregation |
| `/unique-storm/?n=200` | 200 errors with unique fingerprints — demonstrates `NEW_ISSUES_PER_MINUTE` |
| `/task/` | dispatches `demo_app.tasks.fail_task` (eager by default) |
| `/async-boom/` | async view raising |
| `/404/` | `raise Http404` — must not be captured |

`manage.py demo_seed --issues 40 --days 30 [--reset]`: generates realistic data for screenshots.
Uses the real pipeline with `TRANSPORT="sync"` to create issues (raise and capture a set of
exception types with varied culprits), then adjusts `first_seen`/`last_seen`/`count`, creates
`IssueDailyCount` rows with random-walk daily counts, marks some issues resolved/ignored/regressed,
and creates a superuser `admin`/`admin` if missing.

`Makefile` (repo root): `make demo` (migrate, seed, runserver), `make demo-pg` (compose up +
same with `DEMO_DB=postgres`), `make test`, `make test-pg`, `make lint`, `make build`.

Manual QA script (README "Try it"): run `make demo`, open the admin, hit `/boom/` three times →
one issue with count 3; hit `/boom/1/`, `/boom/2/` → one issue; hit `/storm/?n=5000` → response
< 1 s, issue count +5000, ≤ 5 stored events; resolve it in the admin, hit `/boom/` → badge
*regressed* and a console email; run `errors_cleanup --dry-run` → report; switch the browser to dark
mode → UI readable.

---

## 14. Tests

Framework: `pytest` + `pytest-django`, `tests/settings.py` (minimal host, both DB configs selected
by `DJANGO_DB=sqlite|postgres`; Postgres via `ADMIN_ERRORS_TEST_PG_URL` or the compose default
`postgres://postgres:postgres@localhost:5432/admin_errors_test`). Default transport in tests is
`sync`; thread tests opt in. Coverage target ≥ 90 % for `src/admin_errors`, enforced in CI.

### 14.1 Matrix (`tox.ini` + GitHub Actions)

- `lint`: ruff check + ruff format --check.
- `py{310,311,312,313}-dj{42,50,51,52,…}-sqlite` (all combinations Django supports).
- `py313-dj{42,52,latest}-postgres` with a Postgres service container.
- `celery`: latest Python/Django + `celery` installed; runs `tests/test_celery.py` only.
- `package`: `python -m build`, `twine check`, install the wheel into a clean venv, run
  `django-admin check` and `migrate` against the demo settings on SQLite.

### 14.2 Test modules and required cases

`test_fingerprint.py`
- stable hash for fixed inputs (golden values pinned in the test file);
- normalization: numbers, UUIDs, hex, addresses, timestamps, quoted strings, whitespace;
- same exception from two culprits → two fingerprints; two exception types same message → two;
- message-only records use `record.msg` template, not the formatted message;
- explicit `fingerprint` override wins.

`test_capture.py`
- unhandled view exception via `Client` (`raise_request_exception=False`) → one issue, `request`
  context present, `source="exception"`, `logger="django.request"`;
- `logger.exception` inside a view with middleware → request context present; without middleware →
  no request block but still captured;
- `Http404`/`PermissionDenied` ignored; `IGNORE_LOGGERS` exact and prefix; `status_code` < 500 dropped;
- once-per-exception guard: log + re-raise produces one occurrence;
- recursion guard: an exception raised inside `storage` (monkeypatched) does not recurse or raise;
- `BEFORE_SEND` mutates and drops; `ENABLED=False` and `CAPTURE_IN_DEBUG=False` no-ops;
- `capture_message` creates an issue with `source="message"`; `extra` stored;
- `capture_exception()` with no active exception returns `None` and does nothing;
- sampling: with `EVENT_SAMPLE_PER_HOUR=2`, 10 captures → count 10, 2 events;
- new-issue admission: with `NEW_ISSUES_PER_MINUTE=3`, 10 unique errors → 3 issues, stats show 7 dropped;
- payload size degradation order and `MAX_FRAMES`; `MAX_VAR_REPR_LENGTH` truncation;
- async view raising under `AsyncClient` captured with request context.

`test_context.py`
- POST `password` and `@sensitive_post_parameters` scrubbed; `Authorization`, `Cookie`, `X-Api-Key`
  headers scrubbed; `@sensitive_variables` locals scrubbed; `sessionid` cookie scrubbed;
- unrepresentable objects (`__repr__` raising) produce the fallback string;
- `DisallowedHost` while building the absolute URL falls back to path;
- anonymous vs authenticated user; user lookup raising does not break capture;
- `in_app` detection with include/exclude; chained exceptions produce `chain` entries.

`test_storage.py`
- create/update paths; counters and `last_seen`; `first_seen` unchanged on update;
- events ring buffer keeps exactly `EVENTS_PER_ISSUE` newest; `last_event` updated;
- daily counts across midnight (two dates from one batch); UTC date used;
- resolved issue regresses → `open`, `resolved_at` kept, `issue_regressed` fired; ignored issue →
  counters only, no events, no signals;
- `IntegrityError` race: two threads storing the same new fingerprint concurrently → one issue,
  count 2 (`transaction=True`; on Postgres this also proves the savepoint handling);
- `assertNumQueries` upper bounds for new issue with 1 sample, existing issue count-only, and
  existing issue with 3 samples;
- `issue_created` fires after commit only (use `TestCase.captureOnCommitCallbacks`).

`test_writer.py` (uses a fake sink; no DB unless stated)
- lazy start; enqueue aggregates by fingerprint within the interval; flush on batch size; flush on
  explicit `flush()`; queue overflow drops and counts; sink exception swallowed; pid change restarts
  the thread; `atexit` flush is registered once;
- with the real sink and `transaction=True`: an exception raised inside a broken `atomic()` block in
  the test thread is still stored by the writer thread (proves the separate connection).

`test_retention.py`
- each rule in isolation with frozen time (`time_machine` or `unittest.mock` on `timezone.now`);
- eviction order and hysteresis; chunked deletes (`assertNumQueries` bound for 2500 rows);
- SQLite incremental vacuum path runs without error (`auto_vacuum` both 0 and 2 — create the DB with
  `PRAGMA auto_vacuum=INCREMENTAL` in a dedicated test);
- `errors_cleanup` command output, `--dry-run`, `--vacuum` (SQLite only, skipped on Postgres);
- opportunistic cleanup runs at most once per interval per process; respects `CLEANUP="off"`.

`test_admin.py`
- list view renders with cards, badges, sparklines; `assertNumQueries` upper bound for a page of
  50 issues (no N+1);
- filters (status, level, type, last seen) and search; ordering;
- detail view: traceback, chained exceptions, request section, events list, chart; event detail;
- permissions: `view_issue` only → no headers/locals/user in the response; plus
  `view_issue_context` → present; without `change_issue` → status buttons absent and POST → 403;
  without `delete_issue` → delete absent and POST → 403; superuser → everything;
- actions resolve/ignore/reopen (bulk and single) update rows and fire `issue_status_changed`;
- `register(custom_site)` works and `ADMIN_SITE=False` skips default registration;
- pages render with `LANGUAGE_CODE="uk"` (translation loads without errors).

`test_notifications.py`
- new issue → one email to `ADMINS` with subject/body content; regression → email; ignored → none;
- throttle: second occurrence within the window → no email; after the window → email;
- concurrent notifier calls send once (`transaction=True`, two threads);
- `NOTIFY_BACKEND=None` disables; custom backend dotted path is loaded.

`test_celery.py` (celery extra only)
- eager task failure → issue with `celery` context, exactly one occurrence (guard vs. Celery's own
  log record).

`test_settings_and_checks.py`
- defaults; `override_settings` reload; each system check triggers and passes.

`test_routers.py`
- dedicated alias: rows land only in the alias; `migrate` on the default alias creates no
  `admin_errors` tables; `E001` when the alias is missing.

`test_commands.py`
- `errors_test` creates an issue and prints the admin URL; `errors_stats` output.

`test_demo.py`
- with the demo settings on SQLite: every demo URL responds (500 where expected) and produces the
  expected issue/count outcome; `demo_seed` creates the requested number of issues.

`benchmarks/bench_capture.py` (not a test): measures capture-path latency with/without locals and
`store_batch` throughput for 10k aggregated occurrences; prints a table. The README quotes the
numbers from the implementer's machine.

---

## 15. Implementation phases

Each phase ends with: ruff clean, all tests green on SQLite (and Postgres when
`ADMIN_ERRORS_TEST_PG_URL` is set), a `CHANGELOG.md` entry under *Unreleased*, and one commit per
logical unit (`feat(scope): …`, `test(scope): …`, `docs: …`). Do not start the next phase with a
red suite. Do not skip tests "for later".

### Phase 0 — Scaffold

Deliverables: repo layout (Section 4), `pyproject.toml` with `dev`, `celery`, `postgres`
(`psycopg[binary]`) extras, `tox.ini`, GitHub Actions workflow (lint + sqlite matrix + postgres
job + package job), `tests/settings.py` with both DB configs, `conftest.py`, an empty `admin_errors`
app with `AppConfig`, `Makefile`, `README.md` stub, `LICENSE`, `CHANGELOG.md`, `.gitignore`,
`.pre-commit-config.yaml` (ruff).

Acceptance: `tox -e lint,py313-djlatest-sqlite` passes with one placeholder test; the package job
builds a wheel; `make demo` is not yet required.

### Phase 1 — Models, settings, fingerprint, payload, sync capture

Deliverables: `conf.py`, `checks.py` (W001, E002), `models.py` + initial migration, `fingerprint.py`,
`context.py`, `capture.py`, `api.py`, `handlers.py`, `middleware.py`, `storage.py` (full 9.2),
`TRANSPORT="sync"` only, `got_request_exception` marker receiver, `AppConfig.ready()` installing the
handler.

Acceptance: `test_fingerprint`, `test_context`, `test_storage` (except the concurrency case),
`test_capture` (except sampling/admission, which need the process-local buckets — include them here
if trivial), `test_settings_and_checks` for W001/E002 pass on both backends. `assertNumQueries`
bounds documented in the test file.

### Phase 2 — Writer thread, sampling, admission, signals

Deliverables: `writer.py`, sampling and admission buckets, `signals.py`, `issue_created` /
`issue_regressed` wiring, `flush()`, `atexit`, pid check, stats, recursion guard for the writer
thread, `TRANSPORT="thread"` default.

Acceptance: `test_writer` complete including the broken-`atomic()` case and the concurrency case
in `test_storage`; `benchmarks/bench_capture.py` runs and reports capture p50 ≤ 2 ms without
locals on the implementer's machine (record the numbers in `CHANGELOG.md`).

### Phase 3 — Retention, commands, dedicated alias, Celery

Deliverables: `retention.py`, `errors_cleanup`, `errors_stats`, `errors_test`, opportunistic
cleanup in the writer, `tasks.py`, `routers.py` + E001, `integrations/celery.py`, W002.

Acceptance: `test_retention`, `test_commands`, `test_routers`, `test_celery` (tox `celery` env),
remaining checks tests.

### Phase 4 — Admin UI

Deliverables: `admin.py` with `IssueAdmin`, custom URLs/views, actions, `register()`,
template tags, templates, CSS/JS, permission gating, `view_issue_context` permission in the
migration if not already present (it is created in Phase 1's migration — verify).

Acceptance: `test_admin` complete; a manual visual check in the demo (light + dark) documented in
the PR/commit message with a short description of what was verified.

### Phase 5 — Notifications and polish

Deliverables: `notifications.py`, `NOTIFY_*` settings incl. `NOTIFY_BASE_URL`, W003,
`issue_status_changed` from admin, "Copy as text" traceback, i18n markup in templates.

Acceptance: `test_notifications` complete; all templates use `{% translate %}`; `makemessages -l uk`
produces a `.po` with no untranslated-by-design strings left in English UI (translate them).

### Phase 6 — Demo project, docs, release readiness

Deliverables: `demo/` per Section 13, `demo_seed`, `docker-compose.yml`, `Makefile` targets,
`test_demo.py`, README (Section 17 outline), screenshots in `docs/img/` taken from the seeded demo
(light and dark, list and detail), `CHANGELOG.md` `0.1.0`, version bump, `python -m build` +
`twine check` in CI green.

Acceptance: the manual QA script in Section 13 passes end-to-end on SQLite and on the compose
Postgres; `pip install dist/*.whl` into a clean venv followed by `django-admin check` and `migrate`
on a minimal project succeeds.

---

## 16. Gotchas and decisions already made

- **Serialization happens in the capturing thread on purpose.** Traceback frames and locals must be
  read while they are alive and unchanged; the writer thread only does DB I/O. The "does not slow
  down" guarantee refers to DB writes, not to building a ≤ 10 ms payload on a path that is already
  returning a 500.
- **PostgreSQL + IntegrityError**: any create that may race must be inside its own `atomic()`
  savepoint, otherwise the outer transaction is aborted and the following queries fail.
- **SQLite locking**: keep transactions tiny, delete in chunks, set `PRAGMA busy_timeout` is the
  host's job (document `OPTIONS={"timeout": 20}` and WAL in the README); the writer retries once on
  `OperationalError` and then drops.
- **Django test DB and threads**: `TestCase` wraps each test in a transaction invisible to other
  connections; thread-transport tests need `transaction=True`. The in-memory SQLite test DB is
  shared across threads by Django (`mode=memory&cache=shared`); if a thread test is flaky on SQLite,
  switch that test to a file-based test DB via `TEST["NAME"]`.
- **`runserver` autoreloader** runs two processes; the writer must start lazily so both work.
  **gunicorn `--preload`** forks after import; the pid check handles it.
- **`propagate: False`** on `django.request` in the host's `LOGGING` silently disables capture of
  view exceptions — hence check W002 and the explicit handler example in the README.
- **`DummyCache`** makes `cache.add()` always succeed; the process-local timestamp is the real
  limiter for opportunistic cleanup.
- **Do not store settings or environment** in payloads (unlike Django's debug page).
- **Quoted-string normalization** in fingerprints is a deliberate over-merge; document the
  `fingerprint=` override next to it.
- **No `select_for_update`** anywhere (SQLite ignores it; the upsert pattern does not need it).
- **Signals fire in the writer thread**; document loudly. `issue_created` for a count-only first
  item passes `event=None`.
- **Admin `has_change_permission`** must return `True` for users with `change_issue` so the
  change view is reachable, while the form itself stays read-only; the status buttons are the only
  mutation path.
- **Migrations**: exactly one initial migration in 0.1.0; squash before release if development
  created several.

---

## 17. README outline

1. What it is / when to use it instead of Sentry (and when not to).
2. Screenshots (list light/dark, detail).
3. Install: `pip install django-admin-errors`, `INSTALLED_APPS`, `migrate`, optional middleware,
   `LOGGING` snippet, `ADMINS`/email, `errors_test` to verify.
4. How it works: capture → fingerprint → aggregate → bounded storage (one diagram in ASCII).
5. Settings reference (Section 5 table).
6. Permissions and groups.
7. Retention, `errors_cleanup`, Celery beat schedule, dedicated DB alias, SQLite tips (WAL,
   `timeout`, `auto_vacuum`).
8. Notifications and signals; replacing `mail_admins`.
9. Celery / ASGI / gunicorn notes.
10. Storage bound and benchmarks.
11. FAQ: errors not appearing (propagate, DEBUG, level), too many issues, PII concerns, migrating
    from `django-db-log`-style apps.
12. Non-goals and roadmap; contributing (`make test`, `make test-pg`, `make demo`).

---

## 18. Definition of done (0.1.0)

- All phases' acceptance criteria met; CI green on the full matrix.
- Coverage ≥ 90 %; no `# pragma: no cover` on capture/storage paths.
- No runtime dependency other than Django in `pyproject.toml`.
- README complete with screenshots; `CHANGELOG.md` has `0.1.0` with benchmark numbers.
- `errors_test` in a fresh project shows the issue in the admin within one flush interval.
- Storm test (`/storm/?n=5000`) in the demo: response < 1 s on SQLite, DB row count grows by
  ≤ 1 issue + ≤ `EVENT_SAMPLE_PER_HOUR` events + 1 daily count.

---

## Clarifications (pre-run)

Recorded 2026-09-15 during the `/autodev` intake, before the autonomous run started. These outrank the
implementer's own preferences; they do not override any **Default** in Section 5 or any decision in Section 16.

- **Test command contract.** The command the orchestrator runs between phases is `uv run pytest -q` from the
  repository root, with no venv activated. Phase 0 must make this work: `pyproject.toml` declares the `dev`
  extra (pytest, pytest-django, ruff) and `tests/settings.py` + `conftest.py` are wired for it. A `make test`
  wrapper may exist, but it is not the gate.
- **Database gating.** SQLite is the per-phase gate. PostgreSQL gets its own dedicated pass once the storage
  layer is stable (after Phase 3 at the latest, and again before the run ends), using `DJANGO_DB=postgres`
  with `ADMIN_ERRORS_TEST_PG_URL` against the demo `docker-compose.yml` postgres:16 service. Code written
  before that pass must still honour the PostgreSQL rules in Section 16 — savepoints around racing creates,
  no `select_for_update`.
- **End-to-end / browser: on, from Phase 4.** The surface is the demo project of Section 13
  (`http://127.0.0.1:8000/admin/`, superuser `admin`/`admin`). Phases 0–3 have no user-facing surface and the
  e2e step no-ops there. The browser pass also produces the `docs/img/` screenshots (issue list and issue
  detail, light and dark) and runs the manual QA script at the end of Section 13. The e2e up/down commands
  are defined by the architect step once `demo/` exists; up must be idempotent, must run `migrate` and
  `demo_seed` first, and is paired with the ready URL `http://127.0.0.1:8000/admin/login/`.
- **Web access is on** for the single purpose Section 3 requires: verifying the current supported Django
  releases on djangoproject.com so the compatibility and CI matrix are correct at implementation time.
- **PyPI name is free.** `django-admin-errors` is available on PyPI (checked 2026-09-15). The Section 3
  fallback to `django-admin-errorlog` is not needed. Import name stays `admin_errors`. Publishing to PyPI is
  out of scope for this run — build and `twine check` only, no upload.
- **Git delivery.** Commits and pushes go to `korkholeh/django-admin-errors` as `korkholeh`, per phase, on the
  run's own branch. No pull requests are opened and nothing is merged into the default branch.
