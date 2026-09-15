# Review — phase 4 round 1

**Verdict:** changes_requested

Phase 4 delivers the goal: DB I/O is off the request path, the lazily started daemon thread is correct in its core loop (FIFO-ordered flush requests, batch/interval/explicit triggers, put_nowait overflow, pid check), admission and sampling match spec §7.2 step order, and signals fire from on_commit through send_robust. Every acceptance criterion has a named test that actually discriminates, and I re-ran the gates independently: `uv run pytest -q` 139 passed, the full lint command clean, `benchmarks/bench_capture.py` exits 0 with p50 1.046 ms without locals (budget 2 ms) — matching the CHANGELOG numbers. Two defects block: (1) the new two-thread race test fails on Django 4.2, a supported matrix cell, which I reproduced with `uv run tox -e py312-dj42-sqlite` (the DECISIONS entry speculated about this instead of running the 7-second command); (2) the spec-mandated idle connection close is unreachable code, so a worker that saw one error at boot holds its DB connection forever, while CHANGELOG claims the behaviour ships.

## [BLOCKER] New concurrency test fails on Django 4.2, a supported matrix cell
`tests/test_storage.py`

`test_two_threads_storing_one_new_fingerprint_create_one_issue` (tests/test_storage.py:225) only passes because tests/settings.py:47 adds `OPTIONS.transaction_mode = "IMMEDIATE"`, which is gated to Django >= 5.1. On Django 4.2 the guard yields `OPTIONS: {}`, SQLite keeps DEFERRED transactions, and the two writers deadlock upgrading their SHARED locks. Reproduced:

    $ uv run tox -e py312-dj42-sqlite
    FAILED tests/test_storage.py::test_two_threads_storing_one_new_fingerprint_create_one_issue
    E       AssertionError: assert not [OperationalError('database is locked')]
    1 failed, 138 passed in 1.88s
    py312-dj42-sqlite: FAIL code 1 (7.00 seconds)

CLAUDE.md lists Django 4.2 as supported and tox `env_list` has three 4.2 sqlite envs, so `uv run tox` (the documented full-matrix command) is now red. The DECISIONS entry `[p04-implement/tests]` acknowledges the risk but says it was "not re-verified against a real 4.2 interpreter this session" and defers it to "whoever runs that leg" — the command that proves it takes 7 seconds. A test that cannot pass on a declared-supported version is a defect introduced by this phase, not a caveat.

**Fix:** Make the test green on 4.2 rather than skipping it. Django 4.2's sqlite3 backend already runs with `isolation_level=None` and issues the transaction explicitly in `DatabaseWrapper._start_transaction_under_autocommit()` (`cursor.execute("BEGIN")`). Add a test-only backend module (e.g. `tests/sqlite_immediate/base.py`) subclassing `django.db.backends.sqlite3.base.DatabaseWrapper` that overrides that method with `cursor.execute("BEGIN IMMEDIATE")`, and select it as `ENGINE` in tests/settings.py when `django.VERSION < (5, 1)` (keeping `transaction_mode="IMMEDIATE"` on 5.1+). If that is rejected, the fallback is to run this case only against PostgreSQL and say so explicitly — but do not leave a supported tox leg red.

## [MAJOR] Idle connection close is unreachable; the writer holds a DB connection forever
`src/admin_errors/writer.py`

Spec §9.1 ("Connection hygiene") requires `connections[alias].close()` when the thread has been idle for more than 60 s, "do not hold an idle connection open forever". `_time_to_next_flush()` (writer.py:167) returns `None` whenever `batch_started is None`, so `self._queue.get(timeout=None)` at writer.py:120 blocks indefinitely once a batch has been flushed. The idle check at writer.py:163 is therefore only ever evaluated on the same iteration as a flush (where `last_activity` was just reset to `now`) or when a `_WAKE` sentinel from an explicit `flush()` happens to arrive — never on the idle path it exists for. When a real item finally arrives, `aggregates` is non-empty so the `not aggregates` guard skips it.

Proven with `IDLE_CONNECTION_SECONDS` lowered to 0.05 s and a 1 s idle wait (20x the threshold) against a fake `connections` mapping:

    closed after idle: []
    closed after stop: ['default']

The only close came from `stop()` putting `_WAKE`. `grep -rn IDLE_CONNECTION_SECONDS src tests benchmarks` shows the constant is used exactly once and has no test. CHANGELOG.md asserts "the connection is closed after `IDLE_CONNECTION_SECONDS` of inactivity", which is currently false. Impact: a gunicorn worker that captured one error at boot pins a PostgreSQL backend / SQLite file handle for the life of the process.

**Fix:** Bound the blocking wait so the loop can evaluate the idle check. In `_time_to_next_flush`, return the time remaining until the idle deadline instead of `None` when `batch_started is None` (e.g. `max(IDLE_CONNECTION_SECONDS - (time.monotonic() - last_activity), 0.0)`, passing `last_activity` in), or simply `IDLE_CONNECTION_SECONDS` when no batch is pending. Add a `test_idle_connection_is_closed_after_the_idle_window` case that monkeypatches `writer.IDLE_CONNECTION_SECONDS` to a small value and a fake `writer.connections` recording `close()` calls — the probe above is already the test.

## [MINOR] stop() nulls the queue after a timed-out join, crashing the still-running thread
`src/admin_errors/writer.py`

`stop()` (writer.py:219) sets `self._thread = None` and `self._queue = None` unconditionally after `self._thread.join(timeout)`. When the join times out — a slow or wedged sink, exactly the case `test_flush_returns_after_the_timeout_when_the_writer_is_wedged` simulates — the thread is still in `_run` and hits `assert self._queue is not None` (writer.py:118) on its next iteration, or an `AttributeError` on `None.get` under `python -O` where asserts are stripped. `_atexit_flush` calls `stop()`, so this surfaces as an "Exception ignored in thread" traceback at interpreter shutdown whenever the last batch takes longer than 2 s to write.

**Fix:** Only clear `_thread`/`_queue` when `not self._thread.is_alive()` after the join; otherwise leave the references in place (the thread is a daemon and will exit on the shutdown flag) and let the next `_ensure_started()` replace them.

## [MINOR] Writer loop body outside _flush is unguarded: a raise kills the thread and silently drops the batch
`src/admin_errors/writer.py`

Only the sink call is wrapped in try/except (`_flush`, writer.py:174). `_aggregate_item()` (writer.py:125) and `connections[conf.DATABASE].close()` (writer.py:164) run bare. A raise from either — e.g. a host setting `EVENTS_PER_ISSUE = None` makes `len(existing.samples) < conf.EVENTS_PER_ISSUE` a `TypeError`, or `close()` on a broken socket — kills `_run`, discards every aggregate accumulated so far, and increments no counter. The next `enqueue()` transparently restarts the thread via `_ensure_started`, so the loss is invisible; a concurrent `flush()` returns immediately (thread not alive) claiming success. This becomes materially more likely once the idle-close finding above is fixed and `connections[...].close()` actually executes.

**Fix:** Wrap the loop body in `try/except Exception` (re-raising `BaseException` per the capture contract), counting into `stats.batches_dropped`/`stats.last_error` so a dropped batch is at least observable in `errors_stats`.

## [MINOR] Fixed shared SQLite test-DB path, and a comment that describes the opposite of what the setting does
`tests/settings.py`

`TEST.NAME` is a constant path, `os.path.join(tempfile.gettempdir(), "admin_errors_test.sqlite3")` (tests/settings.py:46). All nine sqlite tox envs share it, so `tox -p` / `tox run-parallel` has two interpreters creating and destroying the same file concurrently. The adjacent comment also misleads: it says "`NAME` stays `:memory:` for the common case (fast, no file left behind)" and that `TEST.NAME` "points `django_db(transaction=True)` tests at a real file" — Django uses `TEST["NAME"]` for the whole test database, so every test in the suite now runs file-backed and `:memory:` is used only by non-test invocations such as `django check`.

**Fix:** Qualify the filename with the env, e.g. `f"admin_errors_test_{os.environ.get('TOX_ENV_NAME', 'local')}.sqlite3"`, and reword the comment to say the whole test database is file-backed and why.

## [MINOR] atexit test does not exercise the restart path the guard exists for
`tests/test_writer.py`

PLAN.md T8 specifies "`atexit.register` called exactly once **across a restart**". `test_atexit_flush_is_registered_once` (tests/test_writer.py:237) does two enqueues with the pid unchanged and its own comment says "pid unchanged, no restart, must not re-register". The second `_ensure_started()` returns early at the `is_alive()` check, so `_register_atexit_once()` is never reached a second time — the test passes even if the `_atexit_registered` guard is deleted. The one path that can double-register is `_restart_after_fork()` -> `_ensure_started()`, which is exactly the path left untested. Deviation from the plan, not logged in DECISIONS.

**Fix:** Combine it with the pid-change case: monkeypatch `writer.os.getpid` between the two enqueues as `test_pid_change_restarts_the_thread` does, assert the thread was replaced, and assert `len(calls) == 1`.

## [MINOR] issue_regressed is not actually asserted to fire only after commit
`tests/test_storage.py`

The acceptance criterion is that both `issue_created` and `issue_regressed` "fire only after commit (asserted with `captureOnCommitCallbacks`)". `test_issue_created_fires_only_after_commit` uses `execute=False` and asserts `self.created == []` before running the callbacks — that discriminates. `test_issue_regressed_fires_on_reopen_not_created` (tests/test_storage.py:299) uses `execute=True`, so it proves the signal fires but would pass unchanged if `storage` sent it inline instead of through `transaction.on_commit`.

**Fix:** Switch that case to `captureOnCommitCallbacks(execute=False)`, assert `self.regressed == []`, then run the callbacks and assert the single call — mirroring the `issue_created` case.

## [NIT] Benchmark run command missing from the commands table
`CLAUDE.md`

`benchmarks/bench_capture.py` is now a runnable, budget-gated check (exits 1 on a miss, `--no-gate` to disable) and PLAN.md's Verification block lists `uv run python benchmarks/bench_capture.py`. CLAUDE.md's Commands table has no row for it; `benchmarks/` is only mentioned in the Layout section as "bench_capture.py, not a test".

**Fix:** Add a `benchmark | uv run python benchmarks/bench_capture.py` row to the Commands table.
