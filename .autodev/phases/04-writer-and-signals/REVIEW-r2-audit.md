# Audit of the round-2 fixes — phase 4

**Verdict:** approve

All three REVIEW-r2 findings are genuinely fixed in the product by this diff, none rejected, each logged in DECISIONS. The MAJOR busy-spin fix (writer.py:170-177) wraps the idle close in its own try/except with `finally: last_activity = time.monotonic()`, covering the review's exact ConnectionDoesNotExist repro, and `test_idle_connection_close_failure_does_not_busy_spin` fails without it (batches_dropped would climb into the thousands vs the `<= 3` bound). `_restart_after_fork` now rebinds `_lock`/`_flush_requests`/`_shutdown` before touching state and is called outside any lock, so no nested-lock hazard was introduced. `_aggregate_item` now uses `astimezone(utc).date()`, matching capture.py:363 and spec §9.2 step 6. Verified independently: `uv run pytest -q` 144 passed, full lint command exit 0. Two minor test-quality issues remain: the UTC-date test is tautological on a UTC host and the fork test hangs rather than fails on regression. Neither blocks the commit.

## [MINOR] UTC-date regression test cannot fail on a UTC host
`tests/test_writer.py`

`test_aggregate_item_date_matches_the_sync_paths_utc_expression` asserts `thread_date == ts.astimezone(datetime.timezone.utc).date()` — the same expression the fixed implementation uses. On a machine whose *system* timezone is UTC (typical CI/docker; `datetime.astimezone()` ignores Django's TIME_ZONE), `ts.date()` and `ts.astimezone(utc).date()` are equal, so the test passes against the old buggy `item.timestamp.date()` too. It only discriminates on a non-UTC host (confirmed here: local +0300 gives 2026-09-14 vs 2026-09-15). The product fix is correct; only the guard is environment-dependent, and the review asked for a both-transports comparison.

**Fix:** Pin the reference: construct the timestamp with an explicit fixed offset (e.g. `datetime.datetime(2026, 9, 15, 1, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=3)))`) and assert the bucket date is literally `datetime.date(2026, 9, 14)`, independent of the host's TZ — or compare the resulting `IssueDailyCount` date across the sync and thread paths as the review suggested.

## [MINOR] Fork-lock test hangs the suite instead of failing if the fix regresses
`tests/test_writer.py`

`test_restart_after_fork_rebinds_a_lock_held_at_fork_time` acquires `w._lock` from the test's own thread and then calls `w._restart_after_fork()`. With the fix, the lock is discarded and the test passes. Without it, `with self._lock:` blocks forever in the main pytest thread with no timeout, so a regression wedges the whole run rather than producing a failure. Secondarily, calling `_restart_after_fork()` on a still-live writer leaves the old thread orphaned and reading the *new* `self._queue`/`self._shutdown` — harmless here (both threads exit on `stop()`), but it makes the case a two-consumer race rather than the single-thread child scenario it models.

**Fix:** Drive the restart from a worker thread joined with a timeout and assert on the join (`t.join(2.0); assert not t.is_alive()`), so a regression fails fast instead of hanging; optionally `w.stop()` before simulating the fork so only one writer thread is live.
