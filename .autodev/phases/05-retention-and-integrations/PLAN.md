# Phase 5 — Retention, commands, dedicated alias and Celery

**Goal:** storage stays bounded without operator attention, and the operator has instruments to
inspect, clean and self-diagnose.

## Context

### What already exists (end of Phase 4)

| Module | Relevant state |
|---|---|
| `conf.py` | Frozen 39-key `DEFAULTS`. Every retention key this phase needs is already there: `EVENT_RETENTION_DAYS`, `DAILY_COUNT_RETENTION_DAYS`, `RESOLVED_ISSUE_TTL_DAYS`, `OPEN_ISSUE_TTL_DAYS`, `IGNORED_ISSUE_TTL_DAYS`, `MAX_ISSUES`, `CLEANUP`, `CLEANUP_INTERVAL_SECONDS`, `SQLITE_VACUUM`, `DATABASE`. **No new settings key is added by this phase.** |
| `models.py` | `Issue` (`fingerprint`, `status`, `first_seen`, `last_seen`, `count`, `resolved_at`, `resolved_by` FK → `AUTH_USER_MODEL` with `related_name="+"`, `notified_at`, `last_event`), `Event` (`issue` FK, `timestamp`, `payload`; no reverse relations), `IssueDailyCount` (`issue` FK, `date`, `count`). |
| `checks.py` | `check_settings_keys` (W001), `check_sqlite_version` (E002), both registered from `AppConfig.ready()`. `W002_ID`/`E001_ID` do not exist yet. |
| `apps.py` | `ready()` registers the two checks, installs the logging handler, connects the `got_request_exception` marker receiver. Must not query the DB / start threads / touch the filesystem. |
| `capture.py` | `_build_and_store(...)` → `_dispatch(...)`; `capture_exception_info(exc_type, exc_value, tb, *, request, extra, fingerprint, level)`. `_mark_captured_once()` is the once-per-exception guard. **No `celery=` parameter anywhere on the path.** |
| `context.py` | `build_payload(..., celery: dict | None = None)` — already accepts and emits the `celery` block (spec §6.4). Nothing calls it with `celery=` yet. |
| `writer.py` | `Writer` with lazy thread start, `_run` loop, `_flush`, module-global `stats: Stats`, `reset_for_tests()`. No cleanup hook. |
| `storage.py` | `store_batch(batch, *, using)`. |
| `tests/settings.py` | One alias (`default`); SQLite file-backed `TEST["NAME"]` qualified by `TOX_ENV_NAME`, `BEGIN IMMEDIATE` on every supported Django. `ADMIN_ERRORS = {"TRANSPORT": "sync"}`. |
| `tests/urls.py` | `path("admin/", admin.site.urls)` — the admin **is** mounted, but `admin.py` (model registration) is Phase 8. |
| `tox.ini` | A `celery` env already exists (`extras = celery`, full `pytest -q`). `celery 5.6.3` is installed in the default env by `uv sync --all-extras`, so `tests/test_celery.py` runs in the normal gate too. |

### What this phase changes

New modules: `retention.py`, `routers.py`, `tasks.py`, `integrations/__init__.py`,
`integrations/celery.py`, `management/commands/{errors_cleanup,errors_stats,errors_test}.py`.
Edited: `checks.py` (+E001, +W002), `apps.py` (register the two checks, connect the Celery
integration guardedly), `writer.py` (opportunistic cleanup hook + `stats.cleanups`),
`capture.py` (thread a `celery=` kwarg down to `context.build_payload`), `tests/settings.py`
(second database alias), `tests/conftest.py` (scratch-SQLite-alias fixture), `CHANGELOG.md`.

New tests: `tests/test_retention.py`, `tests/test_commands.py`, `tests/test_routers.py`,
`tests/test_celery.py`; W002 cases appended to `tests/test_settings_and_checks.py`.

## Design

### `retention.py`

```python
CHUNK_SIZE = 1000
HYSTERESIS = 0.9

@dataclasses.dataclass
class CleanupReport:
    alias: str
    dry_run: bool = False
    events: int = 0                  # rule 1
    daily_counts: int = 0            # rule 2
    resolved_issues: int = 0         # rule 3
    ignored_issues: int = 0          # rule 4
    open_issues: int = 0             # rule 5
    evicted_issues: int = 0          # rule 6
    vacuum: str | None = None        # rule 7: "incremental" | "full" | None
    duration_seconds: float = 0.0

    def rows(self) -> list[tuple[str, int]]: ...   # ordered, for the command's output

def run_cleanup(*, using: str | None = None, vacuum: bool = False,
                dry_run: bool = False) -> CleanupReport
```

`using=None` resolves to `conf.DATABASE`, a superset of the spec signature so the writer hook and
the Celery task need no argument. One `now = timezone.now()` is taken at the top and used by every
rule, so a cleanup cannot straddle a clock tick.

The six rules run in spec §9.3 order, each through one helper:

```python
def _delete_in_chunks(queryset, model, *, dry_run: bool) -> int
```

which repeatedly materialises `list(qs.order_by().values_list("pk", flat=True)[:CHUNK_SIZE])` and
deletes `model.objects.using(alias).filter(pk__in=ids)` until the select comes back empty —
a bounded-memory loop, never one list of every id. The count accumulated is
`deleted_per_model.get(model._meta.label, 0)` from `.delete()`'s second return value, so cascaded
child rows are not counted as parents. `dry_run=True` returns `queryset.count()` and deletes
nothing.

Rule details:

1. `Event.timestamp < now - EVENT_RETENTION_DAYS`.
2. `IssueDailyCount.date < (now - DAILY_COUNT_RETENTION_DAYS).date()`.
3. `Issue.status="resolved"` and `Greatest(resolved_at, last_seen) < cutoff` — expressed as
   `Q(resolved_at__lt=cutoff) | Q(resolved_at__isnull=True)` **and** `last_seen__lt=cutoff`, which
   is the same predicate without needing `Greatest` (portable across both backends and NULL-safe).
4. `Issue.status="ignored"` and `last_seen < cutoff`; **skipped entirely when
   `IGNORED_ISSUE_TTL_DAYS is None`**.
5. `Issue.status="open"` and `last_seen < cutoff`.
6. Eviction: only when `MAX_ISSUES` is not `None` and `Issue.objects.count() > MAX_ISSUES`. Target
   is `int(MAX_ISSUES * HYSTERESIS)`; delete `total - target` rows, oldest `last_seen` first
   (`order_by("last_seen", "pk")` — `pk` breaks ties deterministically), walking the statuses in
   the order `ignored → resolved → open` and stopping as soon as the target is reached. Deletes go
   through the same chunk helper. In `dry_run` the number is computed arithmetically from the
   counts rules 3–5 *would* have deleted (`max(0, total - would_delete - target)` when
   `total - would_delete > MAX_ISSUES`), since nothing was actually removed.
7. Vacuum, SQLite only (`connections[alias].vendor == "sqlite"`), never under `dry_run`:
   - `vacuum=True` → `VACUUM` (report `"full"`). Django is in autocommit by default, which is what
     SQLite requires; a `TransactionManagementError`/`OperationalError` here is re-raised so the
     command reports it rather than lying.
   - else if `SQLITE_VACUUM == "incremental"` and `PRAGMA auto_vacuum` returns `2` →
     `PRAGMA incremental_vacuum` (report `"incremental"`). `auto_vacuum` `0` or `1`, or
     `SQLITE_VACUUM == "off"` → no pragma, report `None`.

`run_cleanup` does **not** swallow exceptions: it is called from a command (which must report a
failure) and from the writer (which already has its own `try/except`). It is idempotent — every
rule is "delete rows older than X" — so an interrupted run is finished by the next one.

### Opportunistic cleanup (`writer.py`)

Module-global `_last_cleanup: float | None = None` (process-local, per ARCHITECTURE.md's state
table), cleared by `reset_for_tests()`. `Stats` gains `cleanups: int = 0`.

`Writer._maybe_cleanup()` is called from `_run` immediately after a successful `self._flush(batch)`,
inside the existing `try/except Exception` that already guards the loop body:

```python
def _maybe_cleanup(self) -> None:
    if conf.CLEANUP != "opportunistic":
        return
    interval = conf.CLEANUP_INTERVAL_SECONDS
    now = time.monotonic()
    if _last_cleanup is not None and now - _last_cleanup < interval:
        return
    _last_cleanup = now          # set *before* running: a failing cleanup must not retry-storm
    try:
        acquired = cache.add(CLEANUP_LOCK_KEY, 1, interval)
    except Exception as exc:     # cache backend down → fall back to the process-local timestamp
        stats.last_error = type(exc).__name__
        acquired = True
    if not acquired:
        return
    try:
        retention.run_cleanup(using=conf.DATABASE)
        stats.cleanups += 1
    except Exception as exc:
        stats.batches_dropped += 0   # no batch was lost; only the error is recorded
        stats.last_error = type(exc).__name__
```

`CLEANUP_LOCK_KEY = "admin_errors:cleanup-lock"`. With `DummyCache` `add()` always succeeds and the
process-local timestamp is the only limiter — accepted (spec §16, ARCHITECTURE.md trust boundary).
The first flush in a process runs a cleanup, which is deliberate: a restarted worker cleans once.
`TRANSPORT="sync"` never triggers cleanup, matching "in the writer thread".

`writer` imports `retention` at module level (the pipeline direction capture → writer →
storage/retention is preserved; `retention` imports only `models`/`conf`).

### `routers.py`

```python
class AdminErrorsRouter:
    app_label = "admin_errors"
    def db_for_read(self, model, **hints)  -> str | None
    def db_for_write(self, model, **hints) -> str | None
    def allow_relation(self, obj1, obj2, **hints) -> bool | None
    def allow_migrate(self, db, app_label, model_name=None, **hints) -> bool | None
```

- `db_for_read`/`db_for_write`: `conf.DATABASE` when `model._meta.app_label == "admin_errors"`,
  otherwise `None`. In particular **`None` even when `hints["instance"]` is an admin_errors
  instance** — the router has no business claiming to know where a host's user model lives.
- `allow_relation`: `True` when either object is an admin_errors model (otherwise Django's default
  same-database rule would reject assigning a `default`-alias `User` to `Issue.resolved_by`);
  `None` otherwise.
- `allow_migrate`: `db == conf.DATABASE` when `app_label == "admin_errors"`, else `None` — the
  router never opines about other apps, so a host that points `DATABASE` at an existing alias keeps
  its own migrations working.

Known limitation, **deferred to Phase 9 with a README note**: reading `issue.resolved_by` under a
dedicated alias would query the user table on that alias. Phase 9's admin must render the resolver
without a cross-database join (e.g. from `resolved_by_id` via the default routing). This phase adds
a test pinning `db_for_read(User, instance=issue) is None` so the behaviour is explicit rather than
accidental.

### Checks

```python
E001_ID = "admin_errors.E001"   # ADMIN_ERRORS["DATABASE"] alias not in settings.DATABASES
W002_ID = "admin_errors.W002"   # propagate: False trap
```

- `check_database_alias`: one `Error` when `settings.DATABASE not in django_settings.DATABASES`,
  naming the alias and pointing at `DATABASES`. Never connects to the database.
- `check_logging_propagation`: reads the raw `settings.LOGGING` dict. For each of
  `"django.request"` and `"django"`, warn when that logger's config has `propagate: False` and none
  of its `handlers` resolves to `admin_errors.handlers.AdminErrorsHandler` (looked up in
  `LOGGING["handlers"][name]["class"]`, string comparison plus an `issubclass` check when the
  dotted path imports cleanly). One warning per offending logger name, text naming the logger and
  the fix. `AUTO_INSTALL_LOGGING_HANDLER` does not suppress it — it attaches to the *root* logger,
  which is exactly what `propagate: False` cuts off.

Both are registered in `AppConfig.ready()` next to the existing two.

### Management commands

`management/__init__.py`, `management/commands/__init__.py`, then:

- **`errors_cleanup`** — `--vacuum`, `--dry-run`, `--database ALIAS` (default `conf.DATABASE`).
  Validates the alias against `settings.DATABASES` and raises `CommandError` otherwise. With
  `--vacuum` it first writes a warning to stderr that a full `VACUUM` rewrites the database file
  and holds an exclusive lock. Prints one line per rule in §9.3 order plus the vacuum mode and the
  elapsed time; `--dry-run` prefixes the output with `(dry run — nothing deleted)`.
- **`errors_stats`** — `--database ALIAS`. Prints: row counts for `Issue`/`Event`/`IssueDailyCount`;
  oldest (`min(first_seen)`) and newest (`max(last_seen)`) issue; issue counts per status;
  approximate on-disk size (SQLite: `PRAGMA page_count * PRAGMA page_size`; PostgreSQL:
  `sum(pg_total_relation_size(...))` over the three tables; anything else: `n/a`); and the
  process-local `writer.stats` fields, with an explicit note that they are zero in a freshly
  started management process.
- **`errors_test`** — raises and captures one `AdminErrorsTestError` through the real public API
  (`api.capture_exception()` then `api.flush()`), so every guard, limiter, transport and the writer
  are exercised. Then it looks the issue up by fingerprint and prints its admin URL. When
  `capture_exception()` returns `None` or no row appears, it raises a `CommandError` naming the
  likely cause (`ENABLED=False`, `CAPTURE_IN_DEBUG=False`, `BEFORE_SEND` dropped it, or the
  `NEW_ISSUES_PER_MINUTE` admission limit) — that diagnostic *is* the deliverable (risk 12).

  URL resolution order: `reverse("admin:admin_errors_issue_change", args=[pk])`, then on
  `NoReverseMatch` `reverse("admin:index") + f"admin_errors/issue/{pk}/change/"`, then a plain
  "(admin site not mounted)" note. The middle rung is what makes the command useful **now** —
  `admin.py` does not exist until Phase 8 — and it stays correct afterwards.

### Celery

`integrations/__init__.py` is empty. `integrations/celery.py` imports `celery.signals` at module
level (absolute import; the module is only ever imported when Celery is present) and exposes:

```python
def connect() -> None            # idempotent, dispatch_uid="admin_errors.task_failure"
def on_task_failure(sender=None, task_id=None, exception=None, args=None,
                    kwargs=None, traceback=None, einfo=None, **extra) -> None
```

The receiver calls `capture.capture_exception_info(type(exception), exception,
traceback or exception.__traceback__, celery={"task": getattr(sender, "name", ""), "task_id":
task_id, "args": repr(args)[:500], "kwargs": repr(kwargs)[:500]})`. Verified live against
celery 5.6.3 in eager mode: the signal fires, `traceback` is a real traceback object and `einfo` an
`ExceptionInfo`.

`apps.ready()` connects it behind `importlib.util.find_spec("celery") is not None` — not a bare
`try/except ImportError`, which would also swallow a genuine import error inside our own module.

`capture.py` gains a `celery: dict[str, Any] | None = None` keyword on `_capture_exception`,
`_build_and_store` and `capture_exception_info`, passed straight to `context.build_payload`
(which already accepts it). `api.capture_exception` is **not** changed — the public surface
(ADR 0008) stays as documented; the integration is internal and calls `capture` directly.

Double capture is already solved: Celery's own `celery.app.trace` ERROR log arrives *after*
`task_failure`, and `_mark_captured_once(exc_value)` makes the first path win — asserted in
`test_celery.py`.

`tasks.py` never fails to import:

```python
try:
    from celery import shared_task
except ImportError:
    shared_task = None

if shared_task is not None:
    @shared_task(name="admin_errors.cleanup")
    def cleanup(vacuum: bool = False) -> dict[str, object]:
        return dataclasses.asdict(retention.run_cleanup(using=conf.DATABASE, vacuum=vacuum))
```

### Test-host changes

- `tests/settings.py` gains a second alias `"errors"`, **always SQLite** (file-backed
  `TEST["NAME"]`, qualified by `TOX_ENV_NAME` like the existing one) even when `DJANGO_DB=postgres`.
  That is the exact pattern spec §11.3 recommends (a separate SQLite file next to a Postgres main
  DB), it needs no `CREATEDB` grant on the PG pass, and the router is backend-agnostic. No router
  is installed globally, so the alias is inert for every other test and `django check` stays clean.
- `tests/conftest.py` gains a `sqlite_alias` factory fixture: creates a temp SQLite file, optionally
  applies `PRAGMA auto_vacuum=<n>` **before** any table exists, registers the alias through
  `override_settings(DATABASES={...})`, runs `call_command("migrate", database=alias)`, yields the
  alias, and closes the connection on teardown. It is used by the vacuum cases (the only way to get
  `auto_vacuum=2`, and the only way to run `VACUUM` outside the test transaction) and by the
  `allow_migrate` case.

## Tasks

- [x] **T1** `retention.py`: `CleanupReport`, `_delete_in_chunks`, rules 1–2.
      Files: `src/admin_errors/retention.py`.
      Tests (`tests/test_retention.py`, frozen clock via `mock.patch("django.utils.timezone.now")`):
      events older than `EVENT_RETENTION_DAYS` deleted and newer ones kept; daily counts older than
      `DAILY_COUNT_RETENTION_DAYS` deleted and newer ones kept; 2500 stale events are all deleted
      within a documented `django_assert_max_num_queries(10)` bound (4 selects + 3 fast deletes).
- [x] **T2** Rules 3–5 (resolved / ignored / open TTLs).
      Files: `src/admin_errors/retention.py`.
      Tests: resolved issue past TTL deleted, resolved-but-recently-seen kept (proves the
      `max(resolved_at, last_seen)` predicate, including `resolved_at IS NULL`); ignored issue
      deleted when `IGNORED_ISSUE_TTL_DAYS` is set and kept when it is `None`; open issue past
      `OPEN_ISSUE_TTL_DAYS` deleted, recent open kept; deleting an issue cascades its events and
      daily counts and the report does not count them as issues.
- [x] **T3** Rule 6: eviction with status order and hysteresis.
      Files: `src/admin_errors/retention.py`.
      Tests: `MAX_ISSUES=10` with 2 ignored + 2 resolved + 8 open (12 rows) evicts exactly 3 down to
      `int(10*0.9)=9`, removing both ignored plus the oldest resolved — asserted by surviving pks;
      exactly `MAX_ISSUES` rows evict nothing (boundary); `MAX_ISSUES=None` evicts nothing.
- [x] **T4** Rule 7: vacuum, plus the `sqlite_alias` fixture.
      Files: `src/admin_errors/retention.py`, `tests/conftest.py`.
      Tests: `auto_vacuum=2` → `report.vacuum == "incremental"` and the DB is still queryable;
      `auto_vacuum=0` → `report.vacuum is None`; `SQLITE_VACUUM="off"` → `None`; `vacuum=True` →
      `"full"` and the DB is still queryable; on PostgreSQL (only when `DJANGO_DB=postgres`)
      `vacuum=True` is a no-op returning `None`.
- [x] **T5** `dry_run` across every rule.
      Files: `src/admin_errors/retention.py`.
      Tests: a database with rows matching all six rules reports non-zero per-step counts, deletes
      nothing (row counts unchanged), and performs no vacuum.
- [x] **T6** Opportunistic cleanup in the writer.
      Files: `src/admin_errors/writer.py` (`_maybe_cleanup`, `_last_cleanup`, `stats.cleanups`,
      `reset_for_tests`), `tests/test_retention.py`.
      Tests (monkeypatched `retention.run_cleanup` counter, `LocMemCache`): three consecutive
      `_maybe_cleanup()` calls inside one interval run cleanup once; a call after the interval has
      elapsed runs it again; `CLEANUP="off"` never runs it; a second `Writer` sharing the cache
      does not run it while the lock is held (the multi-process case); a cleanup that raises is
      swallowed, recorded in `stats.last_error`, and does not re-run before the interval.
- [x] **T7** `routers.py` and check `E001`.
      Files: `src/admin_errors/routers.py`, `src/admin_errors/checks.py`, `src/admin_errors/apps.py`,
      `tests/test_routers.py`.
      Tests: `db_for_read`/`db_for_write` return the alias for all three models and `None` for
      `User`, including `db_for_read(User, instance=issue) is None`; `allow_relation` is `True` for
      an issue/user pair and `None` for two foreign models; `allow_migrate` is `True` on the alias,
      `False` on another alias for `admin_errors`, and `None` for other apps; `E001` fires for a
      missing alias and is silent for a configured one.
- [x] **T8** Dedicated-alias integration.
      Files: `tests/settings.py` (add the `errors` alias), `tests/test_routers.py`.
      Tests: with `DATABASE="errors"` + the router installed, capturing an error stores the `Issue`,
      `Event` and `IssueDailyCount` rows in `errors` and leaves `default` empty
      (`django_db(databases=["default", "errors"])`); `migrate` against a fresh `sqlite_alias` with
      the router installed creates no `admin_errors_*` table, and the same migrate without the
      router does create them (the positive half of the pair).
- [x] **T9** Check `W002`.
      Files: `src/admin_errors/checks.py`, `src/admin_errors/apps.py`,
      `tests/test_settings_and_checks.py`.
      Tests: `propagate: False` on `django.request` with no `admin_errors` handler → one `W002`;
      the same config listing an `AdminErrorsHandler` handler → silent; `propagate: False` on
      `django` → `W002`; default settings (no `LOGGING`) → silent.
- [x] **T10** `errors_cleanup`.
      Files: `src/admin_errors/management/__init__.py`,
      `src/admin_errors/management/commands/__init__.py`,
      `src/admin_errors/management/commands/errors_cleanup.py`, `tests/test_commands.py`.
      Tests: prints one line per rule with the counts and actually deletes; `--dry-run` prints the
      counts, says so, and deletes nothing; `--vacuum` against a `sqlite_alias` via `--database`
      prints the lock warning and exits 0; an unknown `--database` raises `CommandError`.
- [x] **T11** `errors_stats`.
      Files: `src/admin_errors/management/commands/errors_stats.py`, `tests/test_commands.py`.
      Tests: prints the three row counts, oldest/newest, per-status counts, an approximate size and
      the writer stats labels; on an empty database it prints zeros without raising.
- [x] **T12** `errors_test`.
      Files: `src/admin_errors/management/commands/errors_test.py`, `tests/test_commands.py`.
      Tests: creates exactly one `Issue` and prints a URL containing
      `/admin/admin_errors/issue/<pk>/change/`; with `ENABLED=False` it raises `CommandError`
      explaining why and creates nothing; under `TRANSPORT="thread"` it still finds the issue
      (proves the `flush()` call), `transaction=True`.
- [x] **T13** Celery capture integration.
      Files: `src/admin_errors/capture.py` (`celery=` kwarg on `_capture_exception`,
      `_build_and_store`, `capture_exception_info`), `src/admin_errors/integrations/__init__.py`,
      `src/admin_errors/integrations/celery.py`, `src/admin_errors/apps.py`,
      `tests/test_celery.py`.
      Tests (`pytest.importorskip("celery")`, eager app): a failing task produces exactly one
      `Issue` with `count == 1` whose payload carries a `celery` block with the task name, task id,
      args and kwargs — and the exception's own `exception` block is intact; long args/kwargs are
      truncated to 500 characters; capturing without the integration (plain `capture_exception`)
      produces no `celery` key.
- [x] **T14** `tasks.py`.
      Files: `src/admin_errors/tasks.py`, `tests/test_celery.py`.
      Tests: `tasks.cleanup` exists and, called directly, runs retention and returns a report dict;
      with `celery` masked out of `sys.modules` and the module reloaded, importing `admin_errors.tasks`
      still succeeds and defines no `cleanup` (restored afterwards).
- [x] **T15** Changelog and final gate.
      Files: `CHANGELOG.md` (*Unreleased* entry covering retention, the three commands, the router,
      E001/W002, the Celery integration and the cleanup task).
      Run the full verification block below; leave the tree lint-clean and green.

## Verification

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run python -m django check --settings=tests.settings
uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings
uv run tox -e celery
```

No migration is expected from this phase (no model change) — `makemigrations --check` proves it.

| Acceptance criterion | Proven by |
|---|---|
| `uv run pytest -q` green | the gate command above |
| Each retention rule tested in isolation with a frozen clock | `test_retention.py::test_rule1_events_older_than_retention_deleted`, `…test_rule2_daily_counts_…`, `…test_rule3_resolved_issue_ttl…`, `…test_rule4_ignored_issue_ttl…` (+ the `None` case), `…test_rule5_open_issue_ttl…` — all under `mock.patch("django.utils.timezone.now")` |
| Eviction order and hysteresis asserted | `test_retention.py::test_eviction_order_ignored_then_resolved_then_open_down_to_hysteresis`, `::test_no_eviction_at_exactly_max_issues` |
| Chunked delete of 2500 rows inside a documented `assertNumQueries` bound | `test_retention.py::test_2500_stale_events_delete_in_chunks_within_query_bound` (`django_assert_max_num_queries(10)`, with the 4+3 breakdown in a comment) |
| `errors_cleanup --dry-run` prints per-step counts and deletes nothing | `test_commands.py::test_errors_cleanup_dry_run_prints_counts_and_deletes_nothing` (+ `test_retention.py::test_dry_run_reports_every_rule_and_deletes_nothing`) |
| `errors_cleanup --vacuum` succeeds on SQLite with `PRAGMA auto_vacuum` 0 and 2 | `test_retention.py::test_incremental_vacuum_runs_when_auto_vacuum_is_2`, `::test_no_incremental_vacuum_when_auto_vacuum_is_0`, `::test_full_vacuum_rewrites_and_database_still_usable`, `test_commands.py::test_errors_cleanup_vacuum_warns_and_succeeds` — all against the always-SQLite `sqlite_alias`, plus `test_retention.py::test_vacuum_is_a_noop_on_postgresql` (runs only under `DJANGO_DB=postgres`) |
| Opportunistic cleanup at most once per `CLEANUP_INTERVAL_SECONDS` per process, never with `CLEANUP="off"` | `test_retention.py::test_opportunistic_cleanup_runs_once_per_interval`, `::test_opportunistic_cleanup_runs_again_after_the_interval`, `::test_opportunistic_cleanup_disabled_with_cleanup_off`, `::test_cache_lock_stops_a_second_process` |
| `errors_test` creates an issue and prints its admin URL | `test_commands.py::test_errors_test_creates_issue_and_prints_admin_url` |
| `errors_stats` prints row counts and writer stats | `test_commands.py::test_errors_stats_prints_counts_and_writer_stats` |
| Rows land only in the dedicated alias | `test_routers.py::test_capture_writes_only_to_the_dedicated_alias` |
| `migrate` on `default` creates no `admin_errors` tables | `test_routers.py::test_migrate_with_router_creates_no_admin_errors_tables` (+ the without-router positive case) |
| A missing alias raises `E001` | `test_routers.py::test_e001_for_missing_database_alias` (+ the configured-alias silent case) |
| `uv run tox -e celery` green: eager failure → one Issue, `celery` context, one occurrence | `test_celery.py::test_eager_task_failure_creates_one_issue_with_celery_context` |
| `propagate: False` on `django.request` with no `admin_errors` handler triggers `W002` | `test_settings_and_checks.py::test_w002_propagate_false_without_admin_errors_handler` (+ the handler-present silent case) |

## Risks

| Row | Touched how |
|---|---|
| **4 — SQLite write-lock contention** | Closed for the retention half: every delete is chunked at 1000 ids with a fresh short statement, so a cleanup never holds a long write lock. `routers.py` delivers the structural fix (move error writes to a second file), and the dedicated-alias integration test proves it routes. |
| **18 — unbounded growth despite the limiters** | Closed: all six rules implemented in order, each tested in isolation with a frozen clock; eviction order and hysteresis pinned; four independent triggers (command, Celery beat, opportunistic writer hook, and the eviction cap itself); `errors_stats` reports the actual numbers. |
| **12 — capture silently disabled in the host** | `W002` turns the `propagate: False` trap into a startup warning, `E001` catches a mistyped alias, and `errors_test` is the "is it wired up?" instrument, including a `CommandError` that names the likely cause when nothing was captured. |
| **3 — capture must never raise** | The writer's cleanup hook sits inside the existing loop guard and has its own `try/except`; a failing `cache.add` falls back to the process-local timestamp instead of propagating. `_last_cleanup` is set before the run, so a persistently failing cleanup cannot spin. |
| **5 — PostgreSQL-only defects** | Rule 3 avoids `Greatest` in favour of a portable NULL-safe `Q` predicate; the vacuum path is gated on `connections[alias].vendor == "sqlite"` and has an explicit PostgreSQL no-op case; nothing in this phase uses `select_for_update`. Phase 6 re-runs the whole suite on PG. |
| **6 — thread-transport flakiness** | No new thread test: `_maybe_cleanup()` is exercised by direct calls with a monkeypatched `run_cleanup`, deterministically, with no sleeps. |
| **15 — scope creep** | The command set is exactly the three the spec names; no admin surface, no notifier, no README work in this phase. |

New, phase-specific risk: **`allow_migrate` cannot be proven against a database pytest-django has
already migrated**, since the test databases are created before any router is installed. The plan
therefore migrates a throwaway SQLite alias inside the test (the `sqlite_alias` fixture) and asserts
on its table list, with the router-absent case as the positive control.

## Out of scope

- `admin.py`, templates, static assets, the `issue_status_changed` transitions and the
  `assertNumQueries` bounds on the admin pages — Phase 8. `errors_test` builds the admin URL by
  path prefix until then.
- `notifications.py` / `EmailNotifier` and check `W003` (`mail_admins` overlap) — Phase 9.
- The PostgreSQL pass itself and `demo/docker-compose.yml` — Phase 6.
- README sections (beat schedule, dedicated-alias recipe, SQLite WAL tips, storage bound, FAQ) and
  the migration squash — Phase 10.
- Rendering `Issue.resolved_by` correctly under a dedicated alias (a cross-database read) — Phase 9,
  flagged above; this phase only pins the router's non-opinion with a test.
- Any new `ADMIN_ERRORS` settings key: the 39-key surface is frozen and sufficient.
