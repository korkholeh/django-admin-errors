# Architecture — django-admin-errors

Design of record. Version 1 · 2026-09-15 · derived from `docs/spec.md` v0.1 and `.autodev/INTAKE.md`.

Source of truth order: `docs/spec.md` → this file and the ADRs in `docs/dev/adr/` → existing code
conventions → `.autodev/PROFILE.md`. Where the spec marks a value **Default**, that value stands and
this document does not re-open it.

---

## Problem

`django-admin-errors` (import name `admin_errors`) is a reusable Django app that records unhandled
exceptions and error-level log records into the host project's own database and shows them inside the
Django admin, Sentry-style: grouped into issues, deduplicated by fingerprint, with traceback, request
context, occurrence counts and 14/30-day trends.

It is for Django projects where Sentry is overkill or not permitted — data residency, air-gapped
deployments, small internal tools — and where the operator already lives in the Django admin. The user
is an operator/developer with staff access to that admin, not an end user of the host product.

It is explicitly **not** an APM: no tracing, no breadcrumbs, no releases, no frontend intake, no alert
rule engine, no multi-tenancy, no HTTP ingest, no REST API (spec §2).

The three things it must not get wrong, in order:

1. **Never break the host.** Capture runs on a path that is already returning a 500. It must never
   raise, never recurse, never block on I/O, and never degrade a healthy request.
2. **Never leak secrets.** Payloads carry request bodies, headers, cookies and frame locals from the
   host's production traffic. Scrubbing must reuse Django's own filter, and rendering must be gated by
   a dedicated permission.
3. **Never grow without bound.** The app writes into the host's production database. An error storm
   must not fill the operator's disk.

## Constraints

| Constraint | Value | Source |
|---|---|---|
| Runtime dependencies | Django only. Celery/psycopg/pytest/ruff are optional or dev-only extras. | spec §3 |
| Python | 3.10 – 3.13 | spec §3 |
| Django | 4.2 LTS, 5.2 LTS, 6.0, 6.1 (see *Support matrix* below) | spec §3 + verified 2026-09-15 |
| Databases | SQLite ≥ 3.9 (JSON1) and PostgreSQL ≥ 13, both first-class and both tested. MySQL/MariaDB must not break on import but is untested and unsupported. | spec §3 |
| Deployment targets | WSGI (gunicorn incl. `--preload`, uwsgi), ASGI (uvicorn/daphne), `runserver` with the autoreloader. | spec §3 |
| Minimal host | Must install, migrate and run with only `django.contrib.{admin,auth,contenttypes,sessions,messages}` installed. No `staticfiles`-only assumptions, no `sites`, no `humanize`. | spec §3 |
| Delivery channel | PyPI wheel + sdist, `src/` layout, hatchling, MIT. Name `django-admin-errors` (verified free). Publishing is out of scope for this run: build + `twine check` only. | spec §3, intake |
| Team | One developer, no on-call. Operability cost is a design input: everything must work with zero configuration beyond `INSTALLED_APPS` + `migrate`. | intake |
| Network | The library makes no outbound network calls at all, except SMTP through the host's configured `EMAIL_BACKEND`. Air-gapped installs are a target environment, so no CDN assets, no telemetry, no update checks. | spec §1, §12.4 |
| Code style | ruff lint + format, line length 100, type hints on all public functions. mypy is not a gate. | spec §3 |
| Language | Code, identifiers, comments, docs and commit messages in English. UI strings are `{% translate %}`-wrapped with a Ukrainian catalogue shipped. | spec §0, §12.4 |

### Support matrix (verified 2026-09-15 on djangoproject.com, as spec §3 requires)

Currently supported upstream series are **5.2 LTS (5.2.17), 6.0 (6.0.8), 6.1 (6.1.1)**. Django 4.2 LTS
reached end of extended support in April 2026 and is **no longer supported upstream**, but spec §3 fixes
it as the floor and it is still the version most conservative deployments run. It is kept as a
best-effort target: tested in CI, but security fixes for Django itself are the host's problem.

Python support per Django series (from the Django install FAQ): 4.2 → 3.10–3.12; 5.2 → 3.10–3.13;
6.0/6.1 → 3.12–3.13 (3.14 also upstream-supported, out of scope here, spec caps at 3.13).

Resulting tox/CI matrix — every legal combination inside the spec's Python range:

| Python | Django |
|---|---|
| 3.10 | 4.2, 5.2 |
| 3.11 | 4.2, 5.2 |
| 3.12 | 4.2, 5.2, 6.0, 6.1 |
| 3.13 | 5.2, 6.0, 6.1 |

Plus: a `lint` env, a `postgres` env (py313 × dj52 and dj61), a `celery` env, and a `package` env.
Assumption: the matrix targets the newest patch release of each series at CI time; series are pinned
(`Django>=6.1,<6.2`), patches are not.

## Shape

One installable Python package, one Django app, one background thread per process, no extra services,
no extra datastore. Everything below lives in `src/admin_errors/`.

The system is a **pipeline with a queue in the middle**. The half before the queue runs on the host's
threads and is optimised for never hurting them. The half after the queue owns all database I/O.

```
  host code / Django / Celery
            │
   ┌────────┴──────────────────────────────────┐   entry points (§7.1)
   │ handlers.AdminErrorsHandler  (root logger)│
   │ api.capture_exception / capture_message   │
   │ integrations.celery (task_failure)        │
   │ middleware.RequestContextMiddleware       │◄── ContextVar: current request
   │ signals.got_request_exception marker      │
   └────────┬──────────────────────────────────┘
            │ exc_info | LogRecord  +  optional request
            ▼
   ══════════════ capture.py — CAPTURING THREAD, must never raise ══════════════
     guards → once-per-exception → fingerprint.py → admission bucket →
     sampling bucket → context.py (payload, scrub, truncate) → BEFORE_SEND →
     size enforcement
            │ CapturedItem(fingerprint, meta, timestamp, payload|None)
            ▼
   ┌─────────────────────────┐        TRANSPORT="sync" bypasses the queue
   │ writer.py  Queue(1000)  │ ─────────────────────────────────┐
   │ daemon thread, own conn │                                  │
   │ aggregate by fingerprint│                                  │
   └────────┬────────────────┘                                  │
            │ dict[fp, Aggregate]                               │
            ▼                                                   ▼
   ══════════════ storage.store_batch(batch, *, using) — WRITER THREAD ══════════
     per aggregate: one short atomic() → upsert Issue → counters →
     bulk-insert Events → trim ring buffer → upsert IssueDailyCount
            │                       │
            │ on_commit             └──► retention.run_cleanup() opportunistically
            ▼
   signals.issue_created / issue_regressed ──► notifications.EmailNotifier (throttled)
            │
            ▼
   ┌──────────────────────────────────────────┐
   │ models: Issue · Event · IssueDailyCount  │  in the host DB (alias ADMIN_ERRORS["DATABASE"])
   └────────┬─────────────────────────────────┘
            │ read-only, except status transitions
            ▼
   admin.py + templates/ + templatetags/ + static/   (Django admin, staff only)
```

### Components and why each exists

| Component | Responsibility | Why it exists as its own unit |
|---|---|---|
| `conf.py` | Resolve `settings.ADMIN_ERRORS` against defaults; re-read on `setting_changed`. | Every other module needs settings; without one proxy, `override_settings` in tests would not reach cached values. |
| `checks.py` | System checks W001–W003, E001–E002. | Misconfiguration (propagate:False, missing alias, old SQLite) silently disables capture; checks turn silence into a startup message. |
| `models.py` | `Issue`, `Event`, `IssueDailyCount` and their indexes/constraints. | The persistent contract; everything else is stateless. |
| `fingerprint.py` | Deterministic sha1 grouping key from exception or log record. | It is a frozen public contract (ADR 0003); isolating it makes stability testable in one file. |
| `context.py` | Build the JSON payload: frames, locals, request, celery, extra; scrub; truncate; enforce size. | The only place that touches untrusted host data. Isolating it concentrates the privacy review and the truncation rules. |
| `capture.py` | The synchronous pipeline of spec §7.2: guards, dedup guard, admission, sampling, payload, enqueue. | The single chokepoint that must never raise. One `try/except BaseException` wraps it; everything upstream can be naive. |
| `api.py` | `capture_exception`, `capture_message`, `flush`. | The documented public surface (ADR 0008). Separate from `capture.py` so internals can move without breaking imports. |
| `handlers.py` | `logging.Handler` subclass — the universal entry point. | Django already logs 5xx through `django.request` with `exc_info` and the request; hooking logging captures views, Celery and arbitrary `logger.error` with one integration. |
| `middleware.py` | `ContextVar` holding the current request (sync + async capable). | `logger.exception()` deep inside a view has no request argument; a ContextVar is the only way to attach one without changing host code. |
| `writer.py` | Bounded queue, daemon thread, per-fingerprint aggregation, flush scheduling, pid check, stats, `flush()`. | The "never slows the request path" pillar (ADR 0002). Its `sink` is injectable so thread mechanics are testable without a DB. |
| `storage.py` | `store_batch()` — every ORM write on the capture path. | Concentrates the race/savepoint/query-budget rules in one file that `assertNumQueries` can pin. |
| `retention.py` | `run_cleanup()` — the seven ordered deletion rules + vacuum, returning a `CleanupReport`. | The bounded-storage pillar (ADR 0006); pure function of time and settings, so it is testable with frozen clocks. |
| `routers.py` | Route `admin_errors` models to a dedicated alias; keep them out of others via `allow_migrate`. | Lets SQLite hosts move error write traffic off the main file — the main mitigation for SQLite write-lock contention. |
| `signals.py` | `issue_created`, `issue_regressed`, `issue_status_changed`. | The extension point that replaces the rejected Slack/Telegram notifiers (spec §2). |
| `notifications.py` | `EmailNotifier`, throttled by a conditional `UPDATE`. | Default value for an operator who never opens the admin; separate class so `NOTIFY_BACKEND` can swap it. |
| `integrations/celery.py`, `tasks.py` | `task_failure` receiver and a `shared_task` cleanup, both behind guarded imports. | Celery must stay optional; a guarded module is the cheapest way to keep the zero-dependency promise. |
| `admin.py` + `templates/` + `templatetags/` + `static/` | The whole read UI plus the three status transitions. | ADR 0005. The only component with a human contract. |
| `management/commands/` | `errors_cleanup`, `errors_test`, `errors_stats`. | The operator's out-of-band controls; `errors_test` is the "is it wired up?" answer in the README FAQ. |
| `apps.py` | `ready()`: install the logging handler, connect the marker receiver and Celery integration, register checks. | Zero-configuration install. Must not start threads or touch the DB. |

Deliberately **not** components: no service layer, no selector layer, no repository abstraction, no
cache layer, no serializer framework, no plugin registry. `storage.py` *is* the repository and
`retention.py` *is* the service; a second indirection would buy nothing.

### Contracts between components

- **`capture` → `writer`**: `CapturedItem(fingerprint: str, meta: dict, timestamp: datetime, payload: dict | None)`.
  `payload is None` means a count-only item. `meta` (type, title, culprit, level, logger) is always
  present and always cheap, so an `Issue` can be created from a count-only item alone.
- **`writer` → `storage`**: `sink(batch: dict[str, Aggregate], *, using: str)` where
  `Aggregate = (meta, count, first_ts, last_ts, samples: list[tuple[datetime, dict]])`. The sink is a
  plain callable; tests substitute a fake.
- **`storage` → DB**: one `transaction.atomic(using=alias)` **per aggregate**, not per batch, so one bad
  row cannot lose a batch. Query budget per aggregate is fixed and pinned by `assertNumQueries` upper
  bounds on both backends (spec §9.2).
- **`storage` → `signals`**: fired via `transaction.on_commit`, never inside the transaction.
- **payload → admin templates**: the versioned JSON document of spec §6.4 (`"v": 1`). Templates must
  render defensively — every key optional, unknown keys ignored (ADR 0004).
- **public Python API**: `admin_errors.api.{capture_exception, capture_message, flush}`,
  `admin_errors.signals.*`, `admin_errors.admin.register(site)`, `admin_errors.__version__`. Everything
  else is internal and may change within 0.x.
- **settings contract**: the single `ADMIN_ERRORS` dict. Unknown keys are a W001 warning, never silently
  ignored, never an error.

### Where state lives

| State | Where | Lifetime |
|---|---|---|
| Issues, events, daily counts | Host database, alias `ADMIN_ERRORS["DATABASE"]` (default `"default"`) | Until retention/eviction deletes it |
| Current request | `contextvars.ContextVar` set by the middleware | One request |
| "Already captured" mark | Attribute `__admin_errors_captured__` on the exception object | The exception's lifetime |
| "Request for this exception" | Attribute `__admin_errors_request__` set by the `got_request_exception` marker | The exception's lifetime |
| Seen-fingerprint LRU (10k), admission token bucket, per-fingerprint sample buckets (LRU 10k) | Process-local, in `capture.py` | Process lifetime; intentionally **not** shared between processes |
| Writer queue, aggregation dict, stats, pid, last-cleanup timestamp | Process-local, in `writer.py` | Process lifetime |
| Recursion guard | `threading.local()` flag; permanently set on the writer thread | Thread lifetime |
| Cleanup lock | `cache.add("admin_errors:cleanup-lock", …)` — advisory only | `CLEANUP_INTERVAL_SECONDS` |
| Notification throttle | `Issue.notified_at`, updated with a conditional `UPDATE` | Persistent |

Process-local rate limiting is a deliberate choice: sampling budgets multiply by the number of worker
processes, and that is accepted (spec §5 says "per process"). The alternative — a shared cache counter —
adds a dependency on a working cache backend on the error path, which is exactly where the cache is most
likely to be the thing that broke.

### Trust boundaries

1. **Host traffic → payload.** Everything in `request`, `frames[*].vars`, `extra` and the exception
   message is attacker-influenced. Mitigations: scrubbing via
   `get_exception_reporter_filter(request)` (so `@sensitive_variables`, `@sensitive_post_parameters`
   and the host's `DEFAULT_EXCEPTION_REPORTER_FILTER` all apply exactly as on the DEBUG 500 page), a
   safe `repr()` that catches everything, hard truncation at every level, and a refusal to store
   settings or environment (spec §16).
2. **Payload → admin HTML.** Stored payloads are rendered back to a browser. All template output uses
   Django autoescaping; no `|safe` on any payload-derived value; the sparkline/chart SVGs are built
   from integers only; no inline `<script>` (CSP-friendly).
3. **Operator → mutation.** The admin is staff-only. Reads need `view_issue`; sensitive context needs
   `view_issue_context` (ADR 0007); status transitions need `change_issue`; deletion needs
   `delete_issue`. Status transitions are POST-only, CSRF-protected, wrapped in `site.admin_view`, and
   permission-checked in the view — not only in the template.
4. **Host-supplied code → our thread.** `BEFORE_SEND` and signal receivers are host code running inside
   our pipeline. Both are wrapped: a raising `BEFORE_SEND` drops the item and is counted; a raising
   receiver is swallowed and counted. Neither can take the process down.
5. **Capture ↔ our own errors.** A failure inside `admin_errors` must not be captured by
   `admin_errors`. Enforced by the thread-local recursion guard plus the writer thread's permanent
   flag, with internal problems reported only to the `admin_errors.internal` logger when
   `INTERNAL_LOGGING` is on.
6. **Process boundary.** After `fork()` (gunicorn `--preload`, uwsgi) the inherited thread does not
   exist but the inherited object does. The pid check on every `enqueue()` is the boundary guard.

## Data

Three models, one aggregate root. App label `admin_errors`, verbose name **Errors**. All timestamps are
timezone-aware UTC; daily buckets use the UTC date of the event timestamp and the admin labels charts
"per day (UTC)".

```
Issue (root, identity = fingerprint)
  │ 1─n  Event            (CASCADE, ring buffer of EVENTS_PER_ISSUE newest)
  │ 1─n  IssueDailyCount  (CASCADE, unique per (issue, date))
  └ FK   resolved_by → AUTH_USER_MODEL (SET_NULL)
```

- **Identity.** `Issue.fingerprint` is the natural key: `CharField(40, unique=True)`, sha1 hex. The
  surrogate `id` is what the admin URLs use. Two processes racing to create the same fingerprint must
  converge to one row — handled by a savepointed create plus an `IntegrityError` retry (spec §9.2).
- **Ownership.** Everything is owned by the host project; there is no tenant, user or team dimension.
  `resolved_by` is the only link into host data and is nullable with `SET_NULL`, so deleting a staff
  user never deletes error history. `AUTH_USER_MODEL` is referenced by setting, never by import.
- **Denormalisation on purpose.** `Issue.count` is the true total including unsampled occurrences —
  it is *not* `events.count()`. `Issue.last_event` duplicates the newest stored payload so the detail
  page still renders after event retention has deleted every `Event` row. Both are stated invariants,
  not accidents.
- **What must never be lost.** Nothing in this app is irreplaceable — it is observability data, and the
  design deliberately drops events under pressure. The one thing that must survive is **operator
  intent**: `status`, `resolved_at`, `resolved_by` and `notified_at`. An issue the operator resolved
  must not silently reappear as new, and an ignored issue must not start emailing again. Consequences:
  status is never reset by the capture path except by the explicit `resolved → open` regression
  transition (which keeps `resolved_at` so the UI can show *regressed*), and eviction removes `ignored`
  and `resolved` issues before `open` ones only because those are the ones the operator has already
  triaged.
- **Ordering and access paths.** `Issue.Meta.ordering = ["-last_seen"]` with
  `Index(["status", "-last_seen"])` serves the default filtered list. `Event` is only ever read by
  issue, newest first — `Index(["issue", "-timestamp"])`. `IssueDailyCount` is read per issue for the
  chart and swept by date for retention — hence both the unique constraint and `Index(["date"])`.
- **Payload schema.** `Event.payload` and `Issue.last_event` hold the versioned document of spec §6.4
  with `"v": 1`. Readers branch on `v` and tolerate missing keys (ADR 0004).
- **Migrations.** Exactly one initial migration ships in 0.1.0; any migrations created during
  development are squashed before release (spec §16). The `view_issue_context` permission is declared
  in `Issue.Meta.permissions` and therefore created by that initial migration.

## Cross-cutting

**Authentication.** None of our own. The app has no public URLs; every view lives under the admin site
and is wrapped in `site.admin_view`, which enforces `is_staff` and the admin login redirect.

**Authorization.** Standard Django model permissions plus one custom permission (ADR 0007):

| Permission | Grants |
|---|---|
| `view_issue` | list, detail, traceback frames without locals |
| `view_issue_context` | request headers/cookies/GET/POST/user, Celery args, frame locals, `extra` |
| `change_issue` | resolve / ignore / reopen (bulk actions and detail buttons) |
| `delete_issue` | delete issues (cascades to events and daily counts) |
| `add_issue` | unused — `has_add_permission` always returns `False` |

`has_change_permission` returns `True` for `change_issue` holders so the change view is reachable, while
every field is in `readonly_fields`; the status buttons are the only mutation path (spec §16). Each
permission is tested in isolation, in both the view and the template, because a permission on a view is
not a permission on a template include.

**Input validation.** Two kinds. (a) *Host data on the capture path* is never trusted and never
validated — it is coerced: safe `repr()`, truncation, JSON-serialisability enforced before enqueue. A
value that cannot be represented becomes `<unrepresentable ClassName>` rather than an exception.
(b) *Operator input in the admin* is limited to the status-transition views and the changelist filters;
transitions accept only the three literal actions `resolve|ignore|reopen` from the URL and are POST-only,
and filters go through Django's own `list_filter` machinery. There are no user-editable model fields.

**Error handling.** The capture path is `try/except BaseException` with `KeyboardInterrupt` and
`SystemExit` re-raised — it swallows everything else and, with `INTERNAL_LOGGING`, logs once per
exception type per process to `admin_errors.internal`. The writer swallows sink exceptions, retries an
`OperationalError` once after 100 ms, then drops the batch and never re-enqueues. Signal receivers and
`BEFORE_SEND` are individually wrapped. The admin path is the opposite: it is ordinary Django code and
is allowed to raise, because a broken admin page should be visible.

**Logging / observability.** The app is a logging *sink*, so it must be quiet by default:
`INTERNAL_LOGGING=False`. When something looks wrong the operator has three instruments —
`manage.py errors_test` (emits one error through the full pipeline and prints the admin URL),
`manage.py errors_stats` (row counts, oldest/newest, approximate size per table, plus `Writer.stats`:
`dropped_queue_full`, `dropped_new_issue`, flush count, last error type), and the system checks. The
README FAQ maps the three common silent failures (`propagate: False`, `DEBUG`, `CAPTURE_LEVEL`) onto
those instruments.

**Configuration and secrets.** One dict, `ADMIN_ERRORS`, in the host's settings; defaults live in
`conf.py`; unknown keys raise W001; `conf.settings` re-reads on `setting_changed` so `override_settings`
works. The library holds **no secrets of its own** — no API keys, no tokens, no DSN. It reads the host's
`EMAIL_*`, `ADMINS` and `DATABASES`, which the host already manages. Nothing is written to disk outside
the database. The repository contains no `.env`; `.gitignore` already excludes `.env*`, `*.sqlite3` and
`local_settings.py`. The one real secret-handling duty is *negative*: keep host secrets **out** of
payloads (trust boundary 1) and out of the `admin_errors.internal` log.

**Internationalization.** All UI strings are `{% translate %}` / `gettext_lazy`. A Ukrainian catalogue
ships in `locale/uk/LC_MESSAGES` (Phase 5). Dates and numbers use Django's own l10n filters. Admin pages
must render under `LANGUAGE_CODE="uk"` without errors — asserted in `test_admin.py`.

**Accessibility.** The UI inherits Django admin markup, so it inherits the admin's semantics. Additional
rules for what we add: status/level badges carry text, never colour alone; the sparkline and the 30-day
chart are inline SVG with `role="img"` and an `<title>`/`aria-label` stating the numbers, and the same
numbers are reachable as text; collapsed library frames and the locals toggle are `<button>` elements
with `aria-expanded`, not `div`s, so keyboard and screen-reader users can operate them; nothing depends
on hover alone (the full timestamp is in `title=` *and* the cell text carries `timesince`). Colours come
from admin CSS variables so contrast follows the admin theme in both light and dark mode.

## Non-functional requirements

Numbers marked **(A)** are assumptions where the spec is silent; they are chosen to be defensible and
testable, and are logged in `.autodev/DECISIONS.md`.

| Dimension | Target |
|---|---|
| Capture latency, no locals, 30-frame traceback | p50 ≤ 2 ms (spec §7.2); p99 ≤ 8 ms **(A)** |
| Capture latency with locals, 30-frame traceback | p50 ≤ 10 ms (spec §7.2) |
| Count-only capture (sampling budget exhausted) | p50 ≤ 0.3 ms **(A)** — it must be cheap enough that a storm is free |
| Dropped capture (guard rejects: ignored logger/exception, `ENABLED=False`) | p50 ≤ 50 µs **(A)** |
| `enqueue()` | Never blocks. `put_nowait`; on a full queue drop the newest and count it. |
| Storm handling | `/storm/?n=5000` returns in < 1 s and adds ≤ 1 issue + ≤ `EVENT_SAMPLE_PER_HOUR` events + 1 daily count (spec §18) |
| Writer throughput | ≥ 5 000 aggregated occurrences/s into the queue **(A)**; `store_batch` of 200 aggregates ≤ 1 s on SQLite **(A)** |
| Query budget, `store_batch` per aggregate | ≤ 3 queries existing-issue count-only; ≤ 7 new issue with 1 sample; ≤ 8 existing issue with 3 samples **(A, upper bounds pinned by `assertNumQueries` on both backends)** |
| Admin issue list, 50 rows | ≤ 12 queries **(A)** and no N+1 on sparklines (one `Prefetch` for 14 days of counts); p95 render ≤ 300 ms with 5 000 issues in the table on SQLite **(A)** |
| Admin issue detail | ≤ 15 queries **(A)**; the 30-day chart is exactly one query |
| Data volume | ≤ `MAX_ISSUES` (5 000) issues; ≤ 5 000 × 20 events; ≤ 5 000 × 90 daily-count rows. Worst case ≈ 6.4 GB theoretical, tens of MB in practice (spec §9.4) |
| Per-payload size | ≤ `MAX_PAYLOAD_BYTES` (64 KiB) hard cap, enforced by degrading in a documented order |
| Concurrency | Up to 32 host worker processes **(A)**, each with one writer thread and one extra DB connection; the design assumes no cross-process coordination |
| Startup cost | `AppConfig.ready()` ≤ 5 ms **(A)**; no DB query, no thread, no file I/O at import or in `ready()` |
| Memory | Writer queue ≤ 1 000 items × ≤ 64 KiB ≈ 64 MiB worst case, ≈ 5–15 MiB typical **(A)**; two 10 000-entry LRUs of 40-byte keys ≈ 1 MiB |
| Asset budget | `admin_errors.css` ≤ 6 KiB, `admin_errors.js` ≤ 3 KiB, zero external requests (spec §12.4) |
| Test coverage | ≥ 90 % of `src/admin_errors`, no `# pragma: no cover` on capture/storage paths (spec §18) |
| Supported browsers | Whatever the Django admin supports; the JS is vanilla ES2019 with no build step **(A)** |
| Clock | Wall clock only (`timezone.now()`); the design tolerates skew because buckets are per-day and `last_seen` is monotone-by-assignment |

## Failure modes

### External dependencies

| Dependency | Failure | Behaviour |
|---|---|---|
| **Database** | Unavailable / connection refused | `store_batch` raises inside the writer thread → swallowed, batch dropped, `stats.last_error` set, counted. The host request path is untouched because it never talks to the DB. |
| | SQLite `database is locked` (`OperationalError`) | One retry after 100 ms, then drop the batch. Never re-enqueue (an unbounded retry queue is how an observability tool takes down its host). Mitigated structurally: tiny transactions, chunked deletes, and the dedicated-alias recommendation. |
| | Disk full | Same as unavailable — writes fail, are counted and dropped. Bounded storage (ADR 0006) is what prevents *us* from being the cause. |
| | Transaction aborted by a racing `IntegrityError` (PostgreSQL) | Every create that can race is inside its own savepoint, so the outer transaction survives and the code re-selects (spec §9.2, §16). No `select_for_update` anywhere. |
| | Connection gone stale in the writer thread | `close_old_connections()` before every flush; close the connection after 60 s idle rather than holding one forever. |
| **SMTP / email backend** | Slow or down | The notifier runs in the writer thread. A hanging SMTP server stalls the writer, not the host — but it does stall error persistence. Mitigation: the notifier is called from an `on_commit` hook after the row is durable, exceptions are swallowed, and the README documents using a queued email backend for production. `notified_at` is set with a conditional `UPDATE` *before* sending would be wrong (it would lose mail on failure) and *after* would double-send; we set it first and accept that a failed send is not retried — an operator who missed one email will still see the issue in the admin. |
| | Duplicate notification from concurrent processes | `UPDATE … SET notified_at=now WHERE id=… AND (notified_at IS NULL OR notified_at < threshold)`; only the process whose UPDATE affected a row sends. Tested with two threads. |
| **Celery** | Not installed | `integrations/celery.py` and `tasks.py` are imported guardedly; absence is normal, not an error. |
| | Double capture (Celery logs the failure *and* fires `task_failure`) | The once-per-exception attribute guard makes whichever path runs first win. Asserted in `test_celery.py`. |
| **Cache** (opportunistic cleanup lock only) | `DummyCache` makes `add()` always succeed | Accepted (spec §16): the process-local timestamp is then the only limiter, so at worst each process cleans once per interval. Cleanup is idempotent, so concurrent cleanups are correct, only wasteful. |
| | Cache backend down | `cache.add` raising is caught; cleanup proceeds on the process-local timestamp alone. |
| **Host code** (`BEFORE_SEND`, signal receivers, `__repr__`, `request.user`) | Raises | Individually wrapped; the item is dropped or the field degrades to a fallback string. Never propagates. |

### Multi-step operations

| Operation | Timeout | Retry | Duplicate | Partial completion | Unknown outcome | Restart mid-flight |
|---|---|---|---|---|---|---|
| **Capture → enqueue** | None (no I/O) | None | The exception guard prevents the same exception object twice; the *same bug* twice is the point. | N/A — a single `put_nowait` | N/A | Item lost. Accepted. |
| **Writer flush → `store_batch`** | No explicit timeout; batches are tiny | One retry on `OperationalError` | `Issue` upsert is idempotent by fingerprint; `IssueDailyCount` upsert is idempotent by `(issue, date)`. `Event` inserts are **not** idempotent — a retried batch could duplicate sampled events, which is why we never re-enqueue after a failed retry. | Per-aggregate transactions mean a failed aggregate loses only itself; the rest of the batch commits. | Counters may be off by the lost batch. Accepted: counts are approximate by design (sampling already makes them so). | Queue contents are lost; at most `FLUSH_INTERVAL_SECONDS` of occurrences plus whatever is in flight. `atexit` → `flush(timeout=2.0)` narrows this to a crash, not a clean shutdown. A `SIGKILL` loses the queue outright. |
| **`fork()` after import** (gunicorn `--preload`) | N/A | N/A | N/A | The child inherits a `Writer` object whose thread does not exist | Detected, not guessed: `enqueue()` compares `os.getpid()` | Child discards inherited queue state and starts a fresh thread. Inherited queued items are lost — accepted. |
| **`runserver` autoreloader** | N/A | N/A | Two processes, two writers, two sets of process-local buckets | N/A | N/A | Lazy start means both work; sampling budget is doubled. Accepted (spec §5 says "per process"). |
| **Retention cleanup** | None | None — the next interval retries | Idempotent: every rule is "delete rows older than X". Two processes cleaning concurrently is correct. | Chunked deletes of 1 000 ids mean an interrupted cleanup leaves a partially-cleaned table; the next run finishes it. | Harmless. | Safe to interrupt at any chunk boundary. |
| **Full `VACUUM`** | Can take minutes and locks the DB | Manual only | N/A | N/A | N/A | Only via `errors_cleanup --vacuum`, never automatic; the command warns first. Incremental vacuum is the automatic path and only runs when `PRAGMA auto_vacuum` is `2`. |
| **Admin status transition** | Request timeout | Operator retries | Idempotent: setting `status=resolved` twice is the same as once | Single UPDATE, no partial state | Operator sees the list again and can read the badge | Nothing in flight. |
| **Aggregate spanning UTC midnight** | N/A | N/A | N/A | One batch legitimately produces two `IssueDailyCount` rows | N/A | Handled explicitly: the writer keys daily counts by the UTC date of each occurrence, at most two dates per aggregate. Tested. |
| **Payload schema older than the reader** | N/A | N/A | N/A | N/A | N/A | Templates branch on `payload["v"]` and tolerate missing keys; an unknown future `v` renders the raw JSON rather than crashing (ADR 0004). |
| **Migrating a host that has data** | N/A | N/A | N/A | N/A | N/A | Only additive migrations after 0.1.0; data lives in the host DB and survives `pip install -U` + `migrate` untouched. |

## Delivery and operations

**Build.** `pyproject.toml` with hatchling, `src/` layout, `version` read from `admin_errors.__version__`.
`python -m build` produces wheel + sdist; `twine check` gates them. Package data (templates, static,
locale) is declared explicitly so the wheel is complete — verified by the `package` tox env, which
installs the wheel into a clean venv and runs `django-admin check` and `migrate` against a minimal
project. Publishing to PyPI is **out of scope for this run** (intake).

**Extras.** `dev` (pytest, pytest-django, ruff, coverage), `e2e` (pytest-playwright), `celery`,
`postgres` (`psycopg[binary]`). The base install has exactly one dependency: Django.

**CI.** GitHub Actions running the tox matrix above: `lint`, the SQLite matrix, a `postgres` job with a
service container, a `celery` job, and a `package` job. Coverage ≥ 90 % is enforced in CI.

**Install in a host project.** `pip install django-admin-errors` → add `"admin_errors"` to
`INSTALLED_APPS` → `migrate` → optionally add `RequestContextMiddleware` → `manage.py errors_test` to
confirm. No settings are required; `ADMIN_ERRORS` is entirely optional.

**Release.** Semantic versioning, `CHANGELOG.md` per release under *Unreleased* then promoted. Exactly
one migration in 0.1.0. The fingerprint algorithm is a frozen contract: changing it duplicates every
existing issue, so it is a breaking change gated behind a major bump and golden-value tests (ADR 0003).

**Upgrade and data survival.** User data is the host's own database, so it survives the upgrade by
construction — there is no app-managed file, cache or external store to migrate. Rules that keep it
that way: additive migrations only; `payload` is versioned so old events stay readable after a schema
change; settings keys are only ever added, and a removed key degrades to a W001 warning rather than a
crash. Downgrade is not supported (standard for Django apps); the mitigation is that no host data is
destroyed by an upgrade, so a downgrade plus `migrate <app> <prev>` is at worst lossy for *our* rows.

**Day-2 operations.** Retention runs opportunistically in the writer thread by default, so the zero-effort
install stays bounded. Operators who want determinism get `manage.py errors_cleanup` (cron) or
`admin_errors.tasks.cleanup` (Celery beat, schedule documented in the README). `errors_stats` reports
actual size. SQLite hosts get a documented tuning recipe: WAL, `OPTIONS={"timeout": 20}`,
`PRAGMA auto_vacuum=INCREMENTAL`, and the dedicated-alias pattern.

**Demo and QA surface.** `demo/` is a full Django project (spec §13) used for manual QA, the README
screenshots and the browser pass. It is not shipped in the wheel. It carries the only
`docker-compose.yml` in the repo (postgres:16), which doubles as the PostgreSQL test target.

## Rejected alternatives

- **Run Sentry (self-hosted) or any external service.** Rejected: the entire premise is the environments
  where that is not allowed or not worth operating. Also pulls in a dependency stack larger than the
  host application.
- **Write the error synchronously in the request path.** Simplest possible shape, and it is kept as
  `TRANSPORT="sync"` for tests and debugging. Rejected as the default because a 500 handler that does a
  DB write inside a possibly-aborted transaction on a possibly-locked SQLite file is exactly the wrong
  thing to add to a request that is already failing. See ADR 0002.
- **Push each error onto Celery.** Rejected: it would make Celery a hard dependency for the core
  feature, and a broken broker (a common cause of the errors we are trying to record) would silently
  discard them.
- **Spool errors to a file and import them with a cron job.** Survives DB outages, which is genuinely
  attractive. Rejected: adds a writable path, file rotation, corruption handling, permissions and a
  second delivery mechanism to an app whose selling point is "just add it to `INSTALLED_APPS`".
- **`multiprocessing` shared memory or a cache backend for sampling budgets.** Would make sampling
  global rather than per-process. Rejected: adds a hard dependency on a working shared resource on the
  error path, for a bound that is already only approximate.
- **Normalise frames, request data and locals into relational tables.** Rejected: 20–50 rows per event,
  a join-heavy read path, and a schema migration every time the payload gains a field. See ADR 0004.
- **A standalone UI (own URLs, or a JS SPA against a REST API).** Rejected: the target user is defined
  as someone who already lives in the Django admin; a separate UI means separate auth, separate
  permissions, a build step, and shipped JS bundles. See ADR 0005.
- **Capture only via the `got_request_exception` signal.** Rejected: it catches view exceptions and
  nothing else — no `logger.error`, no Celery, no management commands. The signal is kept in the far
  smaller role of attaching the request to the exception object.
- **Middleware as the primary capture mechanism.** Rejected: middleware sees only exceptions that
  propagate past it, misses anything a host middleware above it swallows, and would double-capture
  alongside `django.request`. Middleware is kept only for the request `ContextVar`.
- **Always store every occurrence.** Rejected outright: an error loop at 1 000/s would write 1 000
  rows/s into the host's production database. Sampling plus counters is the whole point. See ADR 0006.
- **A shared `errors` database across projects / multi-tenancy.** Explicit non-goal (spec §2); it would
  force a project dimension into every model, index and query.
