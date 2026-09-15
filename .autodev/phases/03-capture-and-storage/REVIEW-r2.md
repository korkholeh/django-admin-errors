# Review — phase 3 round 2

**Verdict:** changes_requested

Round-1's three majors and its minors are genuinely fixed in the product, each with a test that fails without the fix, and I verified the claims independently: 110 passed on the installed Django 5.2 and on 4.2 (where I confirmed `hidden_settings` really lacks `AUTH`, so the new `_ALWAYS_HIDDEN_META_KEYS` check is load-bearing), full lint exit 0, `0001_initial` untouched, no leaked secret anywhere in the end-to-end payload for either the decorated or the undecorated POST view. Every acceptance criterion now maps to a test that can fail. One new major: `test_max_frames_bounds_the_stored_frame_count` cannot fail — I measured the default frame count for that traceback as 1, so `assert len(frames) <= 1` holds with or without `MAX_FRAMES=1`, leaving `context.build_frames`'s `frames[-max_frames:]` truncation untested while PLAN T11 marks it covered. Three minors: the tightened three-sample query bound (9) does not actually trip the per-row-insert regression its own comment claims (observed 7, per-row loop = 9, still passing); `level` can now be stored outside the four `Issue.Level` choices — the CAPTURE_LEVEL root-lowering fix made `"debug"` reachable, and `api.capture_*(level=...)` takes any string, with >10 chars silently killing the capture on PostgreSQL; and the root-logger level mutation in `ready()` is a user-visible logging side effect with no CHANGELOG line.

## [MAJOR] test_max_frames_bounds_the_stored_frame_count cannot fail; MAX_FRAMES truncation is untested
`tests/test_capture.py`

The test raises `ValueError` directly inside its own `try` block, so the traceback has exactly one frame. I measured it: capturing that exception with default settings stores `len(payload["frames"]) == 1`. The assertion `len(event.payload["frames"]) <= 1` therefore holds whether or not `MAX_FRAMES=1` is applied, so `context.build_frames`'s truncation (`frames = frames[-max_frames:]`, context.py:181-183) — including the "keep the innermost N" direction, the part spec section 5 line 168 actually pins — has no test that can fail. Per the rubric a test that cannot fail is a blocker-level defect; I hold it at major because MAX_FRAMES is a PLAN T11 item rather than one of this step's acceptance criteria, but T11 is marked [x] on the strength of this test. The same test also has a dead first line: `fp = api.capture_message("irrelevant for frames")` is immediately overwritten by the `capture_exception` result.

**Fix:** Raise through a chain of nested helper functions (e.g. `def _a(): _b()` ... `def _c(): raise ValueError("boom")`) so the traceback has >= 3 frames. Assert the unbounded case first (`len(frames) >= 3` with default settings), then under `override_settings(ADMIN_ERRORS={"MAX_FRAMES": 1})` assert `len(frames) == 1` and that the kept frame's `function` is the innermost one (`_c`), which pins the direction of the slice. Drop the dead `capture_message` line.

## [MINOR] Three-sample query bound does not trip the per-row insert regression its comment claims
`tests/test_storage.py`

`test_query_budget_existing_issue_with_three_samples` bounds at 9 and the comment states "Bound left at observed + 2 so a per-row insert loop or a per-date N+1 still trips it." I reproduced the observed count with `CaptureQueriesContext`: 7 (and 13 / 5 for the other two, matching the comments). Replacing `Event.objects.bulk_create` with a per-row `save()` loop turns the single insert into three, i.e. 9 queries — exactly the bound, so the test still passes and the regression risk #9 names goes undetected. The aggregate also has only one date, so the per-date N+1 half is not exercised by this test at all.

**Fix:** Tighten the three-sample bound to 8 (observed + 1), which still leaves room for one extra PostgreSQL savepoint statement but fails at 9. Optionally add a second date to the aggregate so the per-date loop is bounded too.

## [MINOR] Stored `level` can fall outside Issue.Level choices and outside the column width
`src/admin_errors/capture.py`

`_capture_record` (capture.py:307) writes `level=record.levelname.lower()` verbatim and `_capture_exception`/`_capture_message` write the caller's `level.lower()` with no mapping. Spec line 207 and `Issue.Level` allow only critical/error/warning/info. The round-1 fix that lowers the root logger to `CAPTURE_LEVEL` (apps.py:47-50) makes `CAPTURE_LEVEL="DEBUG"` a working configuration for the first time, and such records now store `level="debug"` — a value outside the choices, which Phase 8's admin filters and `get_level_display()` will not handle. Worse, `Issue.level` is `max_length=10`: a custom level name or an `api.capture_exception(level="...")` argument longer than 10 characters passes silently on SQLite but raises `DataError` on PostgreSQL inside `store_batch`, which the pipeline's `try/except BaseException` swallows — the capture is lost with no row and no error.

**Fix:** Add a small mapping in `capture.py` (e.g. `_LEVEL_MAP = {"critical": ..., "fatal": "critical", "error": ..., "warn"/"warning": ..., "info": ..., "debug": "info", "notset": "info"}`) and clamp anything unknown to `Issue.Level.ERROR` before putting it in `meta`. Cover it with a `logging.DEBUG` record under `CAPTURE_LEVEL="DEBUG"` and an `api.capture_message(level="somethinglong")` case.

## [MINOR] ready() now mutates the host's root logger level, with no CHANGELOG line
`src/admin_errors/apps.py`

`_install_logging_handler` lowers the root logger's level to `CAPTURE_LEVEL` when it is more verbose (apps.py:47-50). This is the right fix for the round-1 silent no-op, but it is a global side effect on the host's logging: with `CAPTURE_LEVEL="INFO"` or `"DEBUG"`, every third-party library's INFO/DEBUG records start propagating to all of the host's own root handlers (console, files, log aggregation), which the host never asked for by setting an admin_errors key. Harmless at the default `"ERROR"` (40 > root's 30, so nothing changes), but surprising for anyone who lowers it. The CHANGELOG entry for this phase does not mention it.

**Fix:** Add a sentence to the CHANGELOG *Unreleased* entry (and to the `CAPTURE_LEVEL` row whenever docs/user lands in Phase 10) stating that a `CAPTURE_LEVEL` below the root logger's level lowers the root logger, and that hosts wanting to keep their own handlers quiet should raise the level on those handlers instead.

## [MINOR] Multi-valued POST and GET fields are collapsed to their last value
`src/admin_errors/context.py`

`build_request_block` uses `post.dict()` (context.py:294) and `request.GET.dict()` (context.py:288). `QueryDict.dict()` keeps only the last value of each repeated key, so a form with `tags=a&tags=b&tags=c` is stored as `{"tags": "c"}` — the payload silently misrepresents the request that caused the error, which is the one thing a debugging payload has to get right. Scrubbing is unaffected (`cleanse_setting` recurses into lists), so this is fidelity, not privacy.

**Fix:** Use `{k: v if len(v) > 1 else v[0] for k, v in post.lists()}` (and the same for `request.GET`) before the `cleanse_setting` pass, and add a repeated-key case to `tests/test_context.py`.
