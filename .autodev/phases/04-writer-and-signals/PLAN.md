# Phase 4 — Background writer, sampling and signals

**Goal:** move all database I/O off the request path into a lazily started daemon thread, and bound
admission and sampling so a storm costs one issue and a handful of events.

## Context

What exists after Phase 3 (`e8e90a8`):

| File | State |
|---|---|
| `src/admin_errors/conf.py` | All 39 keys. `TRANSPORT` default is already `"thread"`; `QUEUE_MAXSIZE=1000`, `FLUSH_INTERVAL_SECONDS=1.0`, `FLUSH_BATCH_SIZE=200`, `EVENT_SAMPLE_PER_HOUR=10`, `NEW_ISSUES_PER_MINUTE=50`, `EVENTS_PER_ISSUE=20` are defined and unused. |
| `src/admin_errors/capture.py` | The chokepoint: guards → once-per-exception → fingerprint → meta → `context.build_payload` → `BEFORE_SEND` → `_enforce_size` → `_store` (synchronous `storage.store_batch` for **both** transports, see `p03-plan/capture`). `CapturedItem` is declared but never constructed. `_recursion_guard` is a `threading.local()`. |
| `src/admin_errors/storage.py` | `Aggregate(meta, count, first_ts, last_ts, samples, dates)` and `store_batch(batch, *, using)`; per-aggregate `atomic()`, savepointed racing create, ring-buffer trim, daily counts. Fires no signals. |
| `src/admin_errors/api.py` | `capture_exception`, `capture_message`, `flush()` → hard-coded `return None`. |
| `src/admin_errors/signals.py` | Only `mark_request_on_exception` (the `got_request_exception` marker). |
| `src/admin_errors/apps.py` | `ready()` registers checks, installs the root handler, connects the marker. Starts no thread — must stay that way. |
| `tests/` | `test_capture.py` (28 cases, function style, `pytestmark = django_db`), `test_storage.py` (function style, `_agg()` helper, `assertNumQueries` bounds 15 / 7 / 9), `tests/conftest.py` has an autouse fixture resetting `capture._recursion_guard` and `capture._internal_logged_types`. `tests/settings.py` sets `ADMIN_ERRORS = {"TRANSPORT": "sync"}`. |
| `benchmarks/` | Empty directory. `testpaths = ["tests"]`, so nothing under `benchmarks/` is collected. |

What this phase changes:

- New `writer.py` (the queue, the thread, aggregation, stats).
- `capture.py` gains steps 4 and 5 of spec §7.2 (admission, sampling) and a real `_dispatch`
  that enqueues under `TRANSPORT="thread"` instead of writing inline.
- `signals.py` gains the three public signals; `storage.py` fires two of them from `on_commit`.
- `api.flush()` becomes real.
- `benchmarks/bench_capture.py` is written and its numbers go into the CHANGELOG.

Still not shipped: retention, the dedicated alias, commands, Celery, the admin UI, notifications
(`issue_status_changed` is *declared* here, and fired for the first time in Phase 8).

## Design

### Pipeline after this phase

```
capture._build_and_store
  ├─ meta (always cheap: type, title, culprit, level, logger)
  ├─ 4. admission   : seen-LRU(10k) miss + empty NEW_ISSUES_PER_MINUTE bucket → drop, count
  ├─ 5. sampling    : per-fp bucket EVENT_SAMPLE_PER_HOUR → payload, else payload=None
  ├─ 6. payload     : context.build_payload → BEFORE_SEND → _enforce_size   (sampled only)
  └─ 7. _dispatch(CapturedItem)
          ├─ TRANSPORT="thread" → writer.get_writer().enqueue(item)   ── put_nowait, never blocks
          └─ otherwise          → storage.store_batch({fp: Aggregate}, using=DATABASE)

writer thread: Queue.get(timeout=…) → aggregate by fingerprint → flush
  flush = close_old_connections() → sink(batch, using=alias) → [OperationalError: sleep 0.1, retry once] → swallow
  storage.store_batch → transaction.on_commit → signals.issue_created / issue_regressed (send_robust)
```

### `writer.py`

Module-level state (process-local, per the ARCHITECTURE "Where state lives" table):

| Name | Purpose |
|---|---|
| `stats: Stats` | Mutable dataclass: `enqueued`, `dropped_queue_full`, `dropped_new_issue`, `flushes`, `batches_dropped`, `receiver_errors`, `last_error: str \| None`. **Module-global, not per-`Writer`** — `capture.py` increments `dropped_new_issue` without starting a thread, and the counters survive a fork-triggered `Writer` replacement. `Writer.stats` is a property returning it, so `errors_stats` (Phase 5) reads `Writer.stats` as the spec describes. |
| `_writer: Writer \| None` + `get_writer()` | The singleton. |
| `_atexit_registered: bool` | Guards `atexit.register(_atexit_flush)` — registered exactly once per process, at first thread start (never at import, never in `ready()`). |
| `IDLE_CONNECTION_SECONDS = 60.0`, `RETRY_SLEEP_SECONDS = 0.1` | Module constants, not settings (see Decisions). |

`class Writer`:

| Member | Contract |
|---|---|
| `__init__(self, sink=None)` | Stores the sink (default `None` → resolved to `storage.store_batch` at flush time, so `override_settings` and monkeypatching still work). No queue, no thread yet. |
| `enqueue(item: CapturedItem) -> None` | `if self._pid not in (None, os.getpid())` → `_restart_after_fork()`. `_ensure_started()`. `put_nowait`; on `queue.Full` → `stats.dropped_queue_full += 1` and return. Never raises, never blocks. |
| `_ensure_started()` | Under `self._lock`: if the thread is alive, return. Create `queue.Queue(maxsize=conf.QUEUE_MAXSIZE)` (read at start, so tests can override it), store `os.getpid()`, start a `daemon=True` thread named `admin_errors-writer`, register `atexit` once. |
| `_restart_after_fork()` | Drops the inherited queue, aggregation dict and thread reference (the thread does not exist in the child), then `_ensure_started()`. Inherited queued items are lost — accepted (ARCHITECTURE failure table). |
| `_run()` | Sets the permanent recursion flag for this thread (`capture.mark_thread_internal()`, imported inside the function — see Decisions), then loops: `item = queue.get(timeout=_time_to_next_flush())`; `_WAKE` sentinel or timeout → evaluate flush conditions. |
| `flush(timeout=2.0) -> None` | Appends a fresh `threading.Event` to `self._flush_requests` (under a lock), `put_nowait(_WAKE)` (ignoring `Full` — the interval wakes the thread anyway), then `event.wait(timeout)`. Returns after the timeout even if the queue is not drained. No-op (returns immediately) when no thread was ever started. |
| `stop(timeout=2.0)` | Flush, signal the loop to exit, join. Used by `atexit` and by the test reset helper so no thread is left behind. |
| `stats` | Property → the module-global `stats`. |

**Flush conditions** (checked after each `get`/timeout): `FLUSH_BATCH_SIZE` items accumulated, or
`time.monotonic() - batch_started >= FLUSH_INTERVAL_SECONDS`, or a pending flush request, or
shutdown. Pending flush events are collected *before* the sink call and set *after* it returns, so
`flush()` returning means the batch reached the sink.

**Aggregation** — `dict[str, storage.Aggregate]`:

- `meta`: the **first** item of an aggregate wins (all items share a fingerprint, so the metas are
  equivalent by construction; first-wins avoids a dict write per item).
- `count += 1` for every item, count-only included.
- `samples.append((ts, payload))` only when `payload is not None`, capped at `EVENTS_PER_ISSUE`
  newest (`storage` trims to the same bound anyway; the cap bounds *memory*).
- `first_ts = min`, `last_ts = max`, `dates[utc_date] += 1`.

**Connection hygiene / failure handling** in `_flush(batch)`:

1. `django.db.close_old_connections()`.
2. `sink(batch, using=conf.DATABASE)`.
3. `OperationalError` → `time.sleep(RETRY_SLEEP_SECONDS)`, `close_old_connections()`, retry **once**;
   a second failure → swallow, `stats.batches_dropped += 1`, `stats.last_error = type name`.
4. Any other `Exception` → swallow and count, no retry, never re-enqueue (retried `Event` inserts
   are not idempotent — ARCHITECTURE failure table).
5. `BaseException` other than `Exception` (`KeyboardInterrupt`/`SystemExit`) → let the thread die
   rather than spin.
6. After a flush, remember `_last_activity`. When the loop idles longer than
   `IDLE_CONNECTION_SECONDS` with an empty queue, `connections[alias].close()` once and reset the
   timer, so a worker that saw one error at boot does not hold a connection forever.

### `capture.py` — admission and sampling

Two process-local structures, both bounded LRUs of 10 000 entries (`collections.OrderedDict`,
`move_to_end` on hit, `popitem(last=False)` on overflow):

| Name | Shape | Rule |
|---|---|---|
| `_seen_fingerprints` | LRU set | A fingerprint already seen in this process is always admitted. |
| `_new_issue_bucket` | one `_TokenBucket(capacity=NEW_ISSUES_PER_MINUTE, period=60)` | Consulted only on a *miss*. No token → drop the item, `writer.stats.dropped_new_issue += 1`, return `None`. A token → record the fingerprint and admit. |
| `_sample_buckets` | LRU `fingerprint → _TokenBucket(capacity=EVENT_SAMPLE_PER_HOUR, period=3600)` | Token → build the full payload. No token → count-only item (`payload=None`); the issue is still created/counted. |

`_TokenBucket` holds `(tokens: float, last_refill: float)` on `time.monotonic()`; capacity and
period are re-read from `conf` on every call so `override_settings` takes effect immediately and
`tokens` is clamped to the current capacity. Capacity `None` → unlimited; capacity `<= 0` → no token
ever. No lock (see Decisions). `reset_rate_limits()` clears both structures; the autouse conftest
fixture calls it so budgets never leak between tests.

Placement: both checks live in `_build_and_store`, between the meta dict and the payload build — the
one place all three entry points (`_capture_record`, `_capture_exception`, `_capture_message`)
already funnel through, matching spec §7.2 step order exactly.

`_dispatch(item)` replaces `_store`: `TRANSPORT == "thread"` → `writer.get_writer().enqueue(item)`;
anything else (including an unknown value) → the existing inline `storage.store_batch` path, with
`samples=[]` when the item is count-only. `capture.mark_thread_internal()` is added as the public
way for the writer thread to set the permanent recursion flag.

`api.flush(timeout=2.0)` → `writer.get_writer().flush(timeout)` under the thread transport, a no-op
otherwise (every sync capture is already written when it returns).

### `signals.py`

```python
issue_created = Signal()         # issue, event (may be None)
issue_regressed = Signal()       # issue, event
issue_status_changed = Signal()  # issue, old_status, new_status, user
```

plus `send_safely(signal, **kwargs)` — a thin wrapper over Django's own `Signal.send_robust`, which
already isolates each receiver; it counts the returned exceptions into `writer.stats.receiver_errors`
and reports the first one per type through `capture._log_internal_once`. Hand-rolled per-receiver
`try/except` is not written: `send_robust` is the documented primitive.

### `storage.py` — firing after commit

Inside `_store_one`, after the work is queued on the transaction:

- created (the `_create_issue` path that actually inserted, not the race loser) →
  `transaction.on_commit(partial(_fire, signals.issue_created, issue_id, event), using=alias)`
- `reopen is True` → the same with `issue_regressed`
- ignored issues fire nothing.

`_fire` re-reads the `Issue` inside the callback (the row is written with `.update()`, so no
instance exists) **only when `signal.has_listeners()`** — with no receivers connected (the default
until Phase 9) the callback costs nothing and the existing `assertNumQueries` bounds are untouched.
`event` is the last object returned by `Event.objects.bulk_create(...)`, or `None` for a count-only
aggregate (spec §16: "`issue_created` for a count-only first item passes `event=None`").

### `benchmarks/bench_capture.py`

Standalone script (not collected by pytest). `settings.configure()` with a file-backed SQLite in a
`tempfile.TemporaryDirectory`, `django.setup()`, `call_command("migrate")`. Measures, with
`time.perf_counter_ns` and `statistics`:

| Row | Method |
|---|---|
| capture, no locals | `CAPTURE_LOCALS=False`, thread transport with a no-op sink, 30-frame traceback, N=2000 → p50/p95. Budget **p50 ≤ 2 ms**. |
| capture, with locals | same with `CAPTURE_LOCALS=True`. Budget p50 ≤ 10 ms. |
| capture, count-only | sampling budget exhausted. Budget p50 ≤ 0.3 ms. |
| `store_batch` throughput | 10 000 occurrences aggregated into 50 aggregates, wall time and occurrences/s. |

Prints a fixed-width table with a `budget` and an `ok?` column; exits 1 when a budget is missed
(`--no-gate` disables), so the script is usable as a check and not only as prose.

### Honouring the architecture

- ADR 0002 (no DB on the request path) is what this phase delivers: `enqueue` is `put_nowait` with
  no lock held across I/O, no DB call, no blocking.
- Trust boundary 4 (host code in our thread): `BEFORE_SEND` was already wrapped; signal receivers
  are wrapped by `send_robust`.
- Trust boundary 5 / risk 3: the writer thread sets the recursion flag permanently, so anything the
  writer logs at ERROR cannot re-enter capture.
- Trust boundary 6 / risk 11: `os.getpid()` compared on every `enqueue()`.
- ARCHITECTURE "Where state lives": buckets in `capture.py`, queue/aggregation/pid in `writer.py`,
  recursion guard in a `threading.local()` — unchanged. The one adjustment is `stats` (below).

Deviations from `.autodev/ARCHITECTURE.md`, each also appended to `DECISIONS.md`:

1. **`stats` is a module-global in `writer.py`, not an attribute of the `Writer` instance.** The
   architecture lists stats as writer-owned process-local state and the spec has
   `dropped_new_issue` in `Writer.stats`, but that counter is incremented by `capture.py` on a path
   that must not start a thread. A module-global read through `Writer.stats` satisfies both.
2. **`IDLE_CONNECTION_SECONDS` (60 s) and `RETRY_SLEEP_SECONDS` (0.1 s) are module constants, not
   settings keys.** Spec §9.1 states both as fixed numbers and `conf.DEFAULTS` is the frozen 39-key
   surface that `W001` and the Phase 10 README table are tested against.
3. **No lock around the token buckets.** The architecture's risk-3 mitigation explicitly forbids a
   shared lock on the synchronous path; an interleaved read-modify-write can at worst grant or cost
   one extra sample, which is within the "counts are approximate by design" contract.

## Tasks

- [x] **T1: `signals.py` — the three public signals.** Add `issue_created`, `issue_regressed`,
  `issue_status_changed` and `send_safely()` (over `Signal.send_robust`) next to the existing marker
  receiver; update the module docstring. No test of its own yet (T8 covers it through storage).
- [x] **T2: `writer.py` — `Stats`, `Writer`, lazy start, enqueue, pid check.** Queue created at
  start from `conf.QUEUE_MAXSIZE`; `put_nowait` with `dropped_queue_full` counting;
  `_restart_after_fork`; `atexit` registered exactly once; `stop()`/`reset_for_tests()` helpers.
- [x] **T3: `writer.py` — the thread loop.** Aggregation into `storage.Aggregate`, the three flush
  triggers, flush-request events, `close_old_connections()`, `OperationalError` retry-then-drop,
  idle connection close, permanent recursion flag via `capture.mark_thread_internal()`.
- [x] **T4: `capture.py` — `_TokenBucket`, the LRUs, admission and sampling.** Insert steps 4–5 into
  `_build_and_store`; add `reset_rate_limits()` and `mark_thread_internal()`; count drops into
  `writer.stats.dropped_new_issue`.
- [x] **T5: `capture._dispatch` + `api.flush`.** Build a real `CapturedItem`; enqueue under
  `TRANSPORT="thread"`, inline `store_batch` otherwise; `api.flush(timeout)` delegates to the writer.
- [x] **T6: `storage.py` — fire `issue_created` / `issue_regressed` from `on_commit`.** Capture the
  `bulk_create` return for the `event` argument; gate the re-read on `has_listeners()`.
- [x] **T7: `tests/conftest.py` — reset fixture.** Extend the autouse fixture with
  `capture.reset_rate_limits()`, `writer.reset_for_tests()` (stops any running thread) and a stats
  reset, before and after each test, so no budget, counter or thread leaks.
- [x] **T8: `tests/test_writer.py` — fake-sink cases (no DB).** lazy start · aggregation by
  fingerprint within the interval (count-only item folded in: count grows, samples do not) · flush
  on `FLUSH_BATCH_SIZE` · flush on explicit `flush()` · flush on the interval · queue overflow drops
  and counts (`QUEUE_MAXSIZE=1`, sink held on an `Event`) · raising sink swallowed, `last_error` set,
  thread survives and delivers the next batch · `OperationalError` sink called exactly twice then
  dropped · `flush(timeout)` returns within the timeout against a wedged sink · pid change restarts
  the thread · `atexit.register` called exactly once across a restart · samples capped at
  `EVENTS_PER_ISSUE`.
- [x] **T9: `tests/test_writer.py` — the real-sink DB case.** `transaction=True`,
  `TRANSPORT="thread"`: an exception raised inside a deliberately broken `atomic()` block in the
  test thread (which rolls that block back) is still stored by the writer thread after
  `api.flush()`, proving the separate connection.
- [x] **T10: `tests/test_storage.py` — concurrency and signals.** Two threads storing the same new
  fingerprint (`transaction=True`) → exactly one Issue with `count=2`; a
  `class SignalOnCommitTests(TestCase)` asserting `issue_created` fires only inside
  `captureOnCommitCallbacks` (with `event` present, and `None` for a count-only aggregate),
  `issue_regressed` on reopen, nothing for an ignored issue, and a raising receiver not breaking
  storage.
- [x] **T11: `tests/test_capture.py` — sampling and admission.** `EVENT_SAMPLE_PER_HOUR=2` → 10
  captures, `count=10`, exactly 2 Events · `NEW_ISSUES_PER_MINUTE=3` → 10 unique errors, 3 Issues,
  `stats.dropped_new_issue == 7` · a fingerprint already seen is admitted with an empty bucket
  (positive pair for the refusal case) · `EVENT_SAMPLE_PER_HOUR=0` still creates the issue from a
  count-only item · a monkeypatched monotonic clock moved forward one hour refills the bucket ·
  `TRANSPORT="thread"` end to end: a view exception through `Client` + `api.flush()` → one Issue
  (`transaction=True`).
- [x] **T12: `benchmarks/bench_capture.py`.** Four rows, budget column, non-zero exit on a miss.
  Run it and record the numbers.
- [x] **T13: CHANGELOG + gate.** *Unreleased* entry (writer, sampling/admission, signals,
  `api.flush`, the thread transport becoming real) including the measured benchmark numbers; then
  `uv run ruff format .`, the full lint command, `uv run pytest -q`.

## Verification

```bash
uv run pytest -q
uv run pytest -q tests/test_writer.py tests/test_storage.py tests/test_capture.py
uv run python benchmarks/bench_capture.py
uv run ruff check . && uv run ruff format --check . \
  && uv run python -m django check --settings=tests.settings \
  && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings
# no thread or process left behind:
uv run pytest -q && echo "suite exited cleanly"
```

| Acceptance criterion | Proven by |
|---|---|
| Suite green with `TRANSPORT="thread"` shipped and `sync` in tests | `uv run pytest -q`; `test_settings_and_checks.py` already pins the default, `tests/settings.py` keeps `"sync"`; T11's thread-transport case exercises the shipped default explicitly |
| lazy start | `test_writer.py::test_no_thread_until_first_enqueue` |
| aggregation within the interval | `test_writer.py::test_items_with_one_fingerprint_reach_the_sink_as_one_aggregate` |
| flush on batch size | `test_writer.py::test_flush_on_batch_size` |
| flush on explicit `flush()` | `test_writer.py::test_explicit_flush_returns_after_the_sink_saw_the_batch` |
| queue overflow drops and counts | `test_writer.py::test_queue_overflow_drops_and_counts` |
| raising sink swallowed | `test_writer.py::test_raising_sink_is_swallowed_and_the_thread_survives` |
| pid change restarts the thread | `test_writer.py::test_pid_change_restarts_the_thread` |
| `atexit` registered exactly once | `test_writer.py::test_atexit_flush_is_registered_once` |
| broken `atomic()` block, item still stored (separate connection) | `test_writer.py::test_writer_thread_stores_through_its_own_connection` (`transaction=True`) |
| two threads, one new fingerprint → one Issue, `count=2` | `test_storage.py::test_two_threads_storing_one_new_fingerprint_create_one_issue` (`transaction=True`) |
| `EVENT_SAMPLE_PER_HOUR=2` → `count=10`, 2 Events | `test_capture.py::test_event_sampling_caps_stored_events_not_the_count` |
| `NEW_ISSUES_PER_MINUTE=3` → 3 Issues, `dropped_new_issue == 7` | `test_capture.py::test_new_issue_admission_drops_and_counts` |
| `issue_created` / `issue_regressed` fire only after commit | `test_storage.py::SignalOnCommitTests` (`captureOnCommitCallbacks`) |
| a raising receiver does not break storage | `test_storage.py::SignalOnCommitTests::test_raising_receiver_is_swallowed` |
| benchmark prints a table, p50 ≤ 2 ms without locals, numbers in CHANGELOG | `uv run python benchmarks/bench_capture.py` (exits non-zero on a budget miss) + the CHANGELOG entry from T13 |
| `OperationalError` retried once then dropped (spec §9.1) | `test_writer.py::test_operational_error_is_retried_once_then_dropped` |
| `flush(timeout)` cannot hang the caller | `test_writer.py::test_flush_returns_after_the_timeout_when_the_writer_is_wedged` |

## Risks

| Row of `RISKS.md` | What this plan does |
|---|---|
| **#6 thread-transport tests are flaky** (the row this phase owns) | The sink is injectable, so all of T8 runs without a database. Only T9, T10 and one case of T11 touch the DB, each with `transaction=True`. No test sleeps a fixed interval to "wait for the writer": every synchronisation point is a `threading.Event` or `api.flush()`. If a DB thread case is flaky on in-memory SQLite, the documented remedy is a file-backed test DB via `TEST["NAME"]` — never a sleep, never a skip. The conftest fixture stops the thread after every test so no case inherits another's writer. |
| **#11 fork / preload / autoreloader** (closed here) | Lazy start on first `enqueue()`, never at import or in `ready()`; `os.getpid()` compared on every `enqueue()`; `test_pid_change_restarts_the_thread` is the probe. |
| **#3 capture raises, recurses or hangs** | `enqueue` is `put_nowait` and never blocks; the writer thread carries the permanent recursion flag; the sink is wrapped in `try/except`; signal receivers go through `send_robust`. |
| **#4 SQLite write-lock contention** | The retry-then-drop behaviour on `OperationalError` lands here (the structural half — no DB on the request path — is what the whole phase delivers). Chunked deletes stay in Phase 5. |
| **#5 PostgreSQL-only defects** | The two-thread race test is written now so the dedicated PG pass (Phase 6) has its probe ready; no new `select_for_update`, and the racing create keeps its savepoint. |
| **#1 sampling makes the product confusing** | `stats.dropped_new_issue` and `stats.dropped_queue_full` are counted from day one so Phase 5's `errors_stats` can explain a missing error instead of the operator guessing. |
| **#15 scope creep** | `issue_status_changed` is declared and documented but not fired — its only producer is the admin, in Phase 8. |

## Out of scope

- Retention, the opportunistic cleanup hook in the writer loop, `errors_cleanup` / `errors_stats` /
  `errors_test`, `routers.py`, `tasks.py`, the Celery integration and checks `E001` / `W002`
  — Phase 5. The writer loop gets no cleanup call in this phase.
- The PostgreSQL run of the new concurrency tests — Phase 6.
- The admin UI and therefore every `issue_status_changed` emission — Phase 8.
- `notifications.py`, `EmailNotifier`, `notified_at` throttling and the `NOTIFY_*` behaviour —
  Phase 9. This phase only provides the signals they connect to.
- README documentation of the writer, the storage bound and the benchmark numbers — Phase 10 (the
  numbers are recorded in `CHANGELOG.md` here).
