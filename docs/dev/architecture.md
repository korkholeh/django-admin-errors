# Architecture

How `django-admin-errors` is put together, as built and shipped in 0.1.0. The design of record that
preceded the code is `.autodev/ARCHITECTURE.md`; where the two disagree, this document describes the
implementation and *Divergences from the design of record* at the end names each gap.

The expensive, hard-to-reverse decisions live one per file in [`adr/`](adr/) and are linked from the
sections they govern rather than repeated here.

## What the system is

One installable Python package, one Django app, one background thread per process. No extra service,
no extra datastore, no HTTP ingest. Errors go into the host project's own database through the ORM
([ADR 0001](adr/0001-store-errors-in-the-project-database-via-the-orm.md)) and are read back through
the Django admin ([ADR 0005](adr/0005-django-admin-is-the-only-ui.md)).

Three properties drive every trade-off below, in this order:

1. **Never break the host.** Capture runs on a request that is already returning a 500. It must not
   raise, recurse, block on I/O, or slow a healthy request.
2. **Never leak secrets.** Payloads carry the host's production headers, cookies, POST bodies and
   frame locals.
3. **Never grow without bound.** The app writes into the host's production database.

## Shape: a pipeline with a queue in the middle

Everything before the queue runs on the host's own thread and is optimised for never hurting it.
Everything after the queue owns all database I/O
([ADR 0002](adr/0002-background-writer-thread-for-all-database-writes.md)).

```
  host code / Django / Celery
            │
   ┌────────┴──────────────────────────────────┐   entry points
   │ handlers.AdminErrorsHandler (root logger) │
   │ api.capture_exception / capture_message   │
   │ integrations.celery (task_failure)        │
   │ middleware.RequestContextMiddleware       │◄── ContextVar: current request
   │ signals.mark_request_on_exception         │
   └────────┬──────────────────────────────────┘
            │ exc_info | LogRecord  +  optional request
            ▼
   ═════════ capture.py — HOST THREAD, must never raise ═════════
     guards → once-per-exception mark → fingerprint.py → admission bucket →
     sampling bucket → context.py (payload, scrub, truncate) → BEFORE_SEND →
     size enforcement → sanitize_payload
            │ CapturedItem(fingerprint, meta, timestamp, payload | None)
            ▼
   ┌─────────────────────────┐        TRANSPORT="sync" bypasses the queue
   │ writer.py  Queue(1000)  │ ─────────────────────────────────┐
   │ daemon thread, own conn │                                  │
   │ aggregate by fingerprint│                                  │
   └────────┬────────────────┘                                  │
            │ dict[fingerprint, Aggregate]                      │
            ▼                                                   ▼
   ═════════ storage.store_batch(batch, *, using) — WRITER THREAD ═════════
     per aggregate: one short atomic() → upsert Issue → counters →
     bulk-insert Events → trim ring buffer → upsert IssueDailyCount
            │                       │
            │ on_commit             └──► retention.run_cleanup() opportunistically
            ▼
   signals.issue_created / issue_regressed ──► notifications.EmailNotifier (throttled)
            │
            ▼
   ┌──────────────────────────────────────────┐
   │ models: Issue · Event · IssueDailyCount  │  in the alias ADMIN_ERRORS["DATABASE"]
   └────────┬─────────────────────────────────┘
            │ read-only, except the three status transitions
            ▼
   admin.py + templates/ + templatetags/ + static/   (Django admin, staff only)
```

`TRANSPORT="sync"` writes inline instead of enqueuing. It is the default in the test suite
(`tests/settings.py`) and in `demo_seed`, and is a supported production setting for single-worker
deployments.

## Modules

| Module | Responsibility |
|---|---|
| `conf.py` | Cached proxy over `settings.ADMIN_ERRORS` merged with 40 defaults; cleared on Django's `setting_changed`. Read settings through `admin_errors.conf.settings`, never `django.conf.settings.ADMIN_ERRORS` — the proxy is what makes `override_settings` reach cached values. |
| `checks.py` | System checks `W001` (unknown setting key), `W002` (`propagate: False` trap), `W003` (`AdminEmailHandler` overlap), `E001` (`DATABASE` alias not in `DATABASES`), `E002` (SQLite without JSON1). |
| `models.py` | `Issue`, `Event`, `IssueDailyCount`, their indexes and constraints, and the `view_issue_context` permission. |
| `fingerprint.py` | The sha1 grouping key. Frozen public contract ([ADR 0003](adr/0003-fingerprint-is-a-frozen-public-contract.md)), pinned by golden-value tests. Imports only `hashlib`, `re`, `typing`. |
| `context.py` | Builds the versioned JSON payload: frames, locals, request block, chained exceptions; scrubs, truncates, and sanitises text for PostgreSQL. The only module that touches untrusted host data. |
| `capture.py` | The synchronous pipeline and the single chokepoint that must never raise. Owns the process-local seen-fingerprint LRU, the admission bucket and the per-fingerprint sampling buckets. |
| `api.py` | The public surface: `capture_exception`, `capture_message`, `flush`. |
| `handlers.py` | `AdminErrorsHandler`, a `logging.Handler` — the universal entry point, installed on the root logger from `ready()`. |
| `middleware.py` | `RequestContextMiddleware`: a `ContextVar` holding the current request. Sync- and async-capable. |
| `writer.py` | Bounded queue, lazily started daemon thread, per-fingerprint aggregation, flush scheduling, fork detection, `Stats`, `flush()`. Its `sink` is injectable, so thread mechanics are testable without a database. |
| `storage.py` | `store_batch()` — every ORM write on the capture path, in one `atomic()` per aggregate. |
| `retention.py` | `run_cleanup()` — six ordered delete rules plus an optional vacuum, returning a `CleanupReport`. A pure function of time and settings. |
| `routers.py` | `AdminErrorsRouter`: pins the three models to a dedicated alias and keeps them out of others via `allow_migrate`. Opt-in, not installed by default. |
| `signals.py` | `issue_created`, `issue_regressed`, `issue_status_changed`, the `got_request_exception` marker receiver, and `send_safely()` over `Signal.send_robust`. |
| `notifications.py` | `EmailNotifier` and `refresh_connections()`. |
| `textformat.py` | `format_traceback_text()` — plain-text traceback rendering, shared by the notification body and the admin's *Copy as text* button. |
| `integrations/celery.py`, `tasks.py` | The `task_failure` receiver and a `shared_task` cleanup, both behind guarded imports so Celery stays optional. |
| `admin.py`, `templates/`, `templatetags/`, `static/` | The read UI and the three status transitions. The only component with a human contract. |
| `management/commands/` | `errors_cleanup`, `errors_stats`, `errors_test`. |
| `apps.py` | `ready()`: register checks, install the logging handler, connect the marker receiver, refresh notification receivers, connect the Celery integration. |

There is deliberately no service layer, selector layer, repository abstraction, cache layer or plugin
registry. `storage.py` *is* the repository and `retention.py` *is* the service.

## Contracts between components

- **`capture` → `writer`**: `CapturedItem(fingerprint: str, meta: dict[str, str], timestamp:
  datetime, payload: dict | None)`. `payload is None` means a count-only item: the sampling budget
  for that fingerprint is spent, so the occurrence still counts but no `Event` row is written.
  `meta` (type, title, culprit, level, logger) is always present and always cheap, so an `Issue`
  can be created from a count-only item alone.
- **`writer` → `storage`**: `sink(batch: dict[str, Aggregate], *, using: str | None)` where
  `Aggregate = (meta, count, first_ts, last_ts, samples, dates)`. `dates` maps each UTC date to how
  many of `count` fall on it — a batch that spans UTC midnight legitimately produces two
  `IssueDailyCount` rows, and `first_ts`/`last_ts` alone cannot say how to split the count. The sink
  is a plain callable; tests substitute a fake.
- **`storage` → database**: one `transaction.atomic(using=alias)` **per aggregate**, never per
  batch, so one bad row cannot lose a batch. Every create that can race sits in its own savepoint.
  `select_for_update` is used nowhere — SQLite ignores it.
- **`storage` → `signals`**: fired from `transaction.on_commit`, never inside the transaction, and
  only when `signal.has_listeners()` — that guard is what keeps the `assertNumQueries` budgets
  stable when nothing is connected.
- **payload → admin templates**: the versioned JSON document with `"v": 1`
  ([ADR 0004](adr/0004-event-payload-as-a-versioned-json-document.md)). Templates render
  defensively: every key optional, unknown keys ignored, an unknown future `v` renders raw JSON
  rather than raising.
- **Public Python API**: `admin_errors.api.{capture_exception, capture_message, flush}`,
  `admin_errors.signals.*`, `admin_errors.admin.register(site)`, `admin_errors.__version__`.
  Everything else is internal and may change inside 0.x.
- **Settings contract**: the single `ADMIN_ERRORS` dict. An unknown key is a `W001` warning, never
  silently ignored and never an error.

## Data model

```
Issue (root, identity = fingerprint)
  │ 1─n  Event            (CASCADE, ring buffer of EVENTS_PER_ISSUE newest)
  │ 1─n  IssueDailyCount  (CASCADE, unique per (issue, date))
  └ FK   resolved_by → AUTH_USER_MODEL (SET_NULL, related_name="+")
```

- **Identity** is `Issue.fingerprint`, `CharField(40, unique=True)`. The surrogate `id` is what admin
  URLs use. Two processes racing on the same fingerprint converge on one row via a savepointed
  create plus an `IntegrityError` re-select.
- **Denormalisation on purpose.** `Issue.count` is the true total including unsampled occurrences —
  it is *not* `events.count()`, and the two differing is the normal case, not a bug.
  `Issue.last_event` duplicates the newest stored payload so the detail page still renders after
  event retention has deleted every `Event` row.
- **What must survive is operator intent**, not data: `status`, `resolved_at`, `resolved_by`,
  `notified_at`. The capture path never resets status except through the explicit
  `resolved → open` regression transition, which keeps `resolved_at` so the UI can show *Regressed*.
- **Access paths.** `Issue.Meta.ordering = ["-last_seen"]` with `Index(["status", "-last_seen"])`;
  `Event` is only ever read by issue, newest first (`Index(["issue", "-timestamp"])`);
  `IssueDailyCount` is read per issue and swept by date, hence both the unique constraint and
  `Index(["date"])`.
- **Timestamps** are timezone-aware UTC throughout; daily buckets use the UTC date of the event
  timestamp and the admin labels its charts accordingly.
- **Migrations.** Exactly one migration ships (`0001_initial`), pinned by
  `tests/test_docs.py::test_exactly_one_migration_ships` and by `makemigrations --check --dry-run`
  in the lint gate. Only additive migrations after 0.1.0
  ([ADR 0008](adr/0008-zero-dependency-src-layout-package-with-one-migration.md)).

## Where state lives

| State | Where | Lifetime |
|---|---|---|
| Issues, events, daily counts | Host database, alias `ADMIN_ERRORS["DATABASE"]` (default `"default"`) | Until retention or eviction deletes it |
| Current request | `contextvars.ContextVar` set by the middleware | One request |
| "Already captured" mark | `__admin_errors_captured__` attribute on the exception object | The exception's lifetime |
| "Request for this exception" | `__admin_errors_request__`, set by the `got_request_exception` marker | The exception's lifetime |
| Seen-fingerprint LRU, admission token bucket, per-fingerprint sample buckets | Process-local, in `capture.py` | Process lifetime |
| Writer queue, aggregation dict, `Stats`, pid, last-cleanup timestamp | Process-local, in `writer.py` | Process lifetime |
| Recursion guard | `threading.local()` flag, permanently set on the writer thread | Thread lifetime |
| Cleanup lock | `cache.add("admin_errors:cleanup-lock", …)` — advisory only | `CLEANUP_INTERVAL_SECONDS` |
| Notification throttle | `Issue.notified_at`, written with a conditional `UPDATE` | Persistent |

Rate limiting is process-local on purpose: sampling budgets multiply by the number of worker
processes, and that is accepted. The alternative — a shared cache counter — puts a dependency on a
working cache backend directly on the error path, which is exactly where the cache is most likely to
be the thing that broke.

## Security model

Trust boundaries, in the order they are crossed:

1. **Host traffic → payload.** Everything in `request`, `frames[*].vars`, `extra` and the exception
   message is attacker-influenced. Scrubbing is delegated to Django's
   `get_exception_reporter_filter(request)` and never hand-rolled, so `@sensitive_variables`,
   `@sensitive_post_parameters` and the host's own `DEFAULT_EXCEPTION_REPORTER_FILTER` all apply
   exactly as they do on Django's technical 500 page. On top of that: POST fields are cleansed by
   key as well as by decorator, `Authorization`-style headers are redacted identically on every
   supported Django version (4.2 included), `repr()` is wrapped so a raising `__repr__` degrades to
   `<unrepresentable ClassName>`, and every level is hard-truncated. Settings and environment are
   never stored — unlike Django's debug page.
2. **Payload → admin HTML.** Django autoescaping everywhere; no `|safe` on any payload-derived
   value; the sparkline and chart SVGs are built from integers only; no inline `<script>`.
3. **Operator → mutation.** Every view lives under the admin site and is wrapped in
   `site.admin_view`, so `is_staff` and the admin login redirect apply. Status transitions are
   POST-only, CSRF-protected and permission-checked in the view as well as the template.
4. **Host code → our thread.** `BEFORE_SEND` and every signal receiver are host code running inside
   our pipeline; both are individually wrapped, and a raiser is counted and dropped, never
   propagated.
5. **Our own errors.** A failure inside `admin_errors` must not be captured by `admin_errors`:
   thread-local recursion guard, a permanent flag on the writer thread, and loggers under
   `admin_errors.` dropped unconditionally. Internal problems go to the `admin_errors.internal`
   logger only when `INTERNAL_LOGGING` is on.
6. **Process boundary.** After `fork()` the child inherits a `Writer` object whose thread does not
   exist. `os.getpid()` is compared on every `enqueue()`; a mismatch discards the inherited queue and
   starts a fresh thread.

Permissions ([ADR 0007](adr/0007-sensitive-context-behind-a-dedicated-permission.md)):

| Permission | Grants |
|---|---|
| `view_issue` | list, detail, traceback frames without locals, request **method and path** |
| `view_issue_context` | request headers/cookies/GET/POST/body, the user block, Celery args, frame locals, `extra` |
| `change_issue` | resolve / ignore / reopen, as buttons and as bulk actions |
| `delete_issue` | delete issues (cascades to events and daily counts) |
| `add_issue` | unused — `has_add_permission` always returns `False` |

`view_issue_context` is enforced twice: `admin.py` strips the sensitive keys from the payload before
it reaches the template (`_redact_payload`), and each template include re-checks
`perms.admin_errors.view_issue_context`. A permission on a view is not a permission on a template
include, and the *Copy as text* traceback is gated on both halves. `tests/test_admin.py` asserts the
sensitive strings are *absent* from the response body, not merely that a block is hidden.

## Performance contracts

The numbers that are actually asserted, and where:

| Contract | Bound | Asserted in |
|---|---|---|
| Admin issue list, 50 rows | ≤ 12 queries | `tests/test_admin.py` |
| Admin issue detail | ≤ 15 queries | `tests/test_admin.py` |
| `store_batch`, new issue with one sample | ≤ 15 queries | `tests/test_storage.py` |
| `store_batch`, count-only | ≤ 7 queries | `tests/test_storage.py` |
| `store_batch`, existing issue with three samples | ≤ 8 queries | `tests/test_storage.py` |
| 2 500 stale events deleted in chunks | ≤ 12 queries | `tests/test_retention.py` |
| Capture, no locals / with locals / count-only | p50 ≤ 2 ms / 10 ms / 0.3 ms | `benchmarks/bench_capture.py` (gated exit code) |
| `/storm/?n=5000` | < 1 s, ≤ `EVENT_SAMPLE_PER_HOUR` stored events | `tests/test_demo.py`, `e2e/test_manual_qa.py` |

The 14-day sparkline on 50 changelist rows is the obvious N+1 trap; it is served by one `Prefetch`
filtered to the last 14 days, and the ≤ 12 bound is what keeps it that way.

The storage budgets are upper bounds measured with savepoints included, not minimal query counts.
They are deliberately loose enough not to move when Django changes its savepoint emission, and tight
enough to fail on a per-row `save()` loop replacing `bulk_create`.

## Bounded storage

Four independent mechanisms, each cutting a different dimension, so one failing does not remove the
ceiling ([ADR 0006](adr/0006-bounded-storage-by-admission-sampling-and-retention.md)):

1. **Admission** — `NEW_ISSUES_PER_MINUTE` token bucket gates unseen fingerprints. Dropped
   occurrences are counted, never silently lost.
2. **Sampling** — a per-fingerprint `EVENT_SAMPLE_PER_HOUR` bucket gates full payloads. Every
   occurrence still increments `Issue.count`, sampled or not.
3. **Ring buffer** — `EVENTS_PER_ISSUE` newest events kept per issue, trimmed on write.
4. **Retention** — `retention.run_cleanup()`, six ordered rules plus an optional vacuum:

   | Rule | Deletes |
   |---|---|
   | 1 | `Event` rows older than `EVENT_RETENTION_DAYS` |
   | 2 | `IssueDailyCount` rows older than `DAILY_COUNT_RETENTION_DAYS` |
   | 3 | resolved issues past `RESOLVED_ISSUE_TTL_DAYS` |
   | 4 | ignored issues past `IGNORED_ISSUE_TTL_DAYS` (skipped entirely when it is `None`, the default) |
   | 5 | open issues past `OPEN_ISSUE_TTL_DAYS` |
   | 6 | eviction against `MAX_ISSUES` with hysteresis (down to 90 % of the cap), walking ignored → resolved → open, oldest `last_seen` first |

   Rule 7 is the vacuum: SQLite only, never under `--dry-run`, incremental by default and a full
   `VACUUM` only via `errors_cleanup --vacuum`.

Every delete goes through `_delete_in_chunks` (1 000 ids at a time, re-selecting until empty), so a
cleanup never holds one long write lock and an interrupted cleanup is finished by the next run.
Cleanup runs from three places: the `errors_cleanup` command, the writer's opportunistic hook (at
most once per `CLEANUP_INTERVAL_SECONDS` per process, cache-locked across processes, disabled by
`CLEANUP="off"`), and `admin_errors.tasks.cleanup` under Celery beat.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| Database unavailable | `store_batch` raises inside the writer thread → swallowed, batch dropped, `stats.last_error` set. The host request path never touches the database, so it is unaffected. |
| SQLite `database is locked` | One retry after 100 ms, then the batch is dropped. Never re-enqueued — an unbounded retry queue is how an observability tool takes down its host. |
| PostgreSQL `IntegrityError` on a racing create | Each such create is inside its own savepoint, so the outer transaction survives and the code re-selects. |
| Stale writer connection | `close_old_connections()` before every flush; the connection is closed after `IDLE_CONNECTION_SECONDS` (60 s) of idleness and unconditionally when the thread stops. |
| SMTP slow or down | The notifier runs from an `on_commit` hook in the writer thread, and its exceptions are swallowed. It stalls error persistence, not the host. Use a queued email backend in production. |
| Duplicate notification across processes | `UPDATE … SET notified_at = now WHERE id = … AND (notified_at IS NULL OR notified_at < threshold)`; only the process whose UPDATE touched a row sends. `notified_at` is set *before* the send, so a failed send is not retried. |
| Celery not installed | `integrations/celery.py` and `tasks.py` are imported guardedly; absence is normal. |
| Celery double capture (log *and* `task_failure`) | The once-per-exception attribute guard makes whichever path runs first win. |
| Cache backend down or `DummyCache` | `cache.add` raising is treated as "lock acquired"; cleanup falls back to the process-local timestamp. Cleanup is idempotent, so a concurrent cleanup is wasteful, not wrong. |
| `fork()` after import (gunicorn `--preload`) | Detected by the pid check on `enqueue()`; the child starts a fresh thread and discards inherited queue state. |
| Process killed | The queue is lost — at most `FLUSH_INTERVAL_SECONDS` of occurrences plus whatever is in flight. `atexit` runs `flush(timeout=2.0)`, which narrows this to a crash rather than a clean shutdown. |

`AppConfig.ready()` must never query the database, start a thread, or touch the filesystem. The
writer thread starts lazily on the first `enqueue()` — that is what makes `runserver`'s autoreloader
and gunicorn `--preload` safe.

One `ready()` side effect is worth knowing: when `CAPTURE_LEVEL` is below the root logger's
effective level, `_install_logging_handler` lowers the **root logger** so those records can reach
the handler at all. Records that then propagate to the host's other root handlers (console, file,
aggregator) are a visible consequence. A host that wants its own handlers quiet should raise the
level on those handlers rather than rely on the root logger's level.

## Divergences from the design of record

`.autodev/ARCHITECTURE.md` was written before the code. Where the built system differs, the built
system is the truth:

| Design of record | As built | Decision |
|---|---|---|
| `Aggregate = (meta, count, first_ts, last_ts, samples)` | Sixth field `dates: dict[date, int]` | `p03-plan/storage` — `first_ts`/`last_ts` name the two dates a midnight-spanning batch touches but not how to split `count` between the two `IssueDailyCount` rows. |
| `retention.run_cleanup()` has "seven ordered deletion rules + vacuum" | Six delete rules; the vacuum is rule 7 | `p05-plan/retention` |
| 39 settings keys | 40 — `NOTIFY_BASE_URL` was added with the notifier | `p09/plan` |
| `store_batch` budgets: ≤ 3 count-only, ≤ 7 new issue with one sample | ≤ 7 and ≤ 15 as asserted | `p03-review_fix1/tests` — the original figures did not account for savepoint statements; the asserted bounds were tightened from generous placeholders against measured counts, not relaxed from the design. |
| `ready()` installs the handler | It also lowers the root logger level when `CAPTURE_LEVEL` is below it | `p03-review_fix1/apps` — without it a lower `CAPTURE_LEVEL` is silently unreachable. |
| Notification receivers connected in `ready()` | Connected *and disconnected* dynamically by `notifications.refresh_connections()`, from `ready()` and on `setting_changed` | `p09/plan` — always-on receivers would make `storage._fire`'s `has_listeners()` check untruthful and move every query budget. |
| `context.py` scrubs and truncates | It also sanitises text: NUL → U+FFFD and lone surrogates stripped, recursively over every string value *and* key | `p06-plan/context` — PostgreSQL rejects NUL bytes in `text`/`jsonb`, so an exception message containing one was unstorable. |
| `textformat.py` not present | Added, shared by the notification body and the admin's *Copy as text* | `p09/plan` |
| Writer closes its connection after idle timeout | Also closes it unconditionally when the thread stops | `p06-implement/T7` — a leaked PostgreSQL session made `DROP DATABASE` fail intermittently at the end of a test run. |

Two things that did **not** diverge, and are worth saying so: no `select_for_update` appears
anywhere in `src/`, and no savepoint rule needed changing when the PostgreSQL pass ran — the
discipline held from Phase 3.

## Testing and delivery

| Layer | Command | What it covers |
|---|---|---|
| Unit / integration | `uv run pytest -q` | The whole library against SQLite; the gate for every change |
| PostgreSQL | `make test-pg` | Same suite against `postgres:16`, plus `tests/test_postgres.py` |
| Coverage | `uv run pytest -q --cov=admin_errors --cov-report=term-missing` | ≥ 90 % is enforced in CI; no `# pragma: no cover` on capture/storage paths |
| Browser | `make e2e-up && uv run --extra e2e pytest e2e -q && make e2e-down` | The spec's manual-QA script, driven against `demo/` |
| Packaging | `uv run tox -e package` | Builds the wheel, installs it into a clean venv, runs `django-admin check` + `migrate`, then `tests/package_smoke.py` to prove templates, static files and the `uk` catalogue resolve from inside the installed wheel |
| Matrix | `uv run tox` | 11 Python × Django cells plus `lint`, two `postgres` cells, `celery` and `package` |

`docs/dev/adr/` holds the eight accepted decisions. `docs/spec.md` is the implementation
specification the whole thing was built from; it is the most detailed reference for any behaviour
this document summarises.
