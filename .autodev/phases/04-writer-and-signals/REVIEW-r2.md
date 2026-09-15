# Review — phase 4 round 2

**Verdict:** changes_requested

Phase 4 meets its goal and all six r1 findings are genuinely fixed — verified independently: `uv run pytest -q` 141 passed, `uv run tox -e py312-dj42-sqlite` 141 passed (the r1 blocker's Django-4.2 deadlock is gone, fixed properly via the `tests.sqlite_immediate` BEGIN IMMEDIATE backend rather than a skip), the full lint command exit 0, and `benchmarks/bench_capture.py` exit 0 with p50 1.029 ms no-locals against a 2 ms budget, matching the CHANGELOG numbers. Every acceptance criterion maps to a test that can actually fail: lazy start, aggregation, batch/interval/explicit flush, overflow counting, swallowed sink, pid restart, atexit-once (now on the only double-registering path), the broken-`atomic()` separate-connection case, the two-thread race, sampling 10/2 and admission 3/7, both signals under `captureOnCommitCallbacks(execute=False)`, and the raising receiver. All 13 PLAN tasks are `[x]` and match the diff; the three ARCHITECTURE deviations are logged in DECISIONS. One new major: the r1 idle-close fix introduced a busy-spin — a repeatably failing `connections[alias].close()` never advances `last_activity`, so the loop turns over ~890k times/s (measured) at 100% CPU with `batches_dropped` climbing forever. Two minors: a fork-inherited lock that can hang the child's first capture, and a local-vs-UTC date divergence between the thread and sync paths.

## [MAJOR] Failing idle-connection close makes the writer thread busy-spin at 100% CPU forever
`src/admin_errors/writer.py`

The r1 fix made the idle branch reachable, but `last_activity` (writer.py:165) is only updated on the success path *inside* the try. If `connections[conf.DATABASE].close()` raises repeatedly, the `except Exception` at writer.py:166 counts it and loops without advancing `last_activity`, so `_time_to_next_flush()` returns `max(IDLE - stale_elapsed, 0.0) == 0.0`, `queue.get(timeout=0.0)` returns `Empty` immediately, and the loop re-enters the idle branch at once. Reproduced against a `connections` mapping whose `__getitem__` raises (exactly what `ADMIN_ERRORS["DATABASE"]` naming a missing alias does — `ConnectionDoesNotExist`; check E001 for that only lands in Phase 5):

    $ DJANGO_SETTINGS_MODULE=tests.settings uv run python - <<'EOF'
    ... writer.connections = Boom(); writer.IDLE_CONNECTION_SECONDS = 0.05
    ... w.enqueue(item); w.flush(); time.sleep(1.0)
    batches_dropped after 1s idle: 890327 last_error: KeyError

890k iterations in one second: a pegged CPU core per worker for the life of the process, an unbounded `stats.batches_dropped` that makes Phase 5's `errors_stats` unreadable, and no log line. Note the counter name is also wrong for this path — no batch was dropped.

**Fix:** Advance the idle clock whether or not the close succeeded, e.g. wrap only the close: `try: connections[conf.DATABASE].close() finally: last_activity = time.monotonic()`, or set `last_activity = time.monotonic()` in the `except Exception` handler. Add a regression test alongside `test_idle_connection_is_closed_after_the_idle_window` with a `_FakeConnections.__getitem__` that raises, asserting `stats.batches_dropped` stays small (e.g. ≤ 3) after ~20 idle windows.

## [MINOR] `_restart_after_fork` acquires a lock that may have been held at fork time — first capture in the child can hang forever
`src/admin_errors/writer.py`

`_ensure_started()` takes `self._lock` on every `enqueue()`, and `_run` takes it to swap `_flush_requests`. A `threading.Lock` held at `os.fork()` stays locked forever in the child, and `_restart_after_fork` (writer.py:99) does `with self._lock:` before rebuilding state. If the fork lands in that (small) window, the child's first `enqueue()` blocks permanently — on the request path, inside a `try/except BaseException` that cannot help because a deadlock is not an exception. This is precisely the fork scenario (multiprocessing, celery prefork, uwsgi) that PLAN.md lists as risk #11 "closed here", and ARCHITECTURE's contract is that capture never hangs.

**Fix:** Rebind the lock before using it on the fork path: make `_restart_after_fork` do `self._lock = threading.Lock()` first (and reset `self._flush_requests = []`, `self._shutdown = threading.Event()`), then `_ensure_started()`. Same one-liner applies to `writer._writer_lock` if you want `get_writer()` fork-safe too.

## [MINOR] Daily-count date is local, not UTC, under the thread transport — diverges from the sync path
`src/admin_errors/writer.py`

`_aggregate_item` uses `date = item.timestamp.date()` (writer.py:246) while the inline path in `capture._dispatch` uses `timestamp.astimezone(datetime.timezone.utc).date()`, and `storage.Aggregate`'s docstring plus spec §9.2 step 6 both say the keys are UTC dates. With `USE_TZ=True` (the test host, Django 5+ default) `timezone.now()` is UTC-aware and the two agree, so no test catches this. With `USE_TZ=False` — still the Django 4.2 default, and 4.2 is a supported cell — `timezone.now()` is naive local time and the two transports bucket `IssueDailyCount` on different days for occurrences near midnight (e.g. naive 2026-09-15 01:00 at UTC+3 → `.date()` 2026-09-15 vs `.astimezone(utc).date()` 2026-09-14), skewing the 14/30-day trends by a day.

**Fix:** Use the same expression in both places: `item.timestamp.astimezone(datetime.timezone.utc).date()` in `_aggregate_item` (or, better, compute the UTC date once in `capture._dispatch` and carry it on `CapturedItem`). Add one case asserting the `IssueDailyCount` date is identical for the same timestamp under both transports with `USE_TZ=False`.
