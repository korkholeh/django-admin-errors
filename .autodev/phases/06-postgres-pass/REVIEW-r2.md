# Review — phase 6 round 2

**Verdict:** approve

Phase 6 delivers its goal and every acceptance criterion maps to something I re-ran myself: `make test-pg` green at 216 passed with 0 skipped (so the two-thread same-fingerprint race and all three `assertNumQueries` budgets really executed on PostgreSQL), container torn down with `docker ps` empty, SQLite at 208 passed/8 skipped, ruff/django-check/makemigrations clean, `grep -rn "select_for_update(" src/` empty, `make e2e-up` no-opping with exit 0, and the capture benchmark still at p50 1.466 ms under the 2 ms spec budget despite the new payload walk. All four round-1 findings are genuinely fixed, not papered over: `_sanitize_walk` now sanitizes dict keys and the walk moved to a single late `context.sanitize_payload()` call at `capture.py:439` (after the `extra` merge, `BEFORE_SEND` and `_enforce_size`), with a PG case for a NUL query-param name and a backend-agnostic case for a NUL `extra` key; the writer's shutdown close is a `finally` around the whole loop with a regression test for the non-`break` death path; `test-pg` gates on `pg-up &&`; the ARCHITECTURE.md/spec.md connection-hygiene wording and the T10 note are corrected. The fingerprint contract (ADR 0003) is untouched — `fp` is computed by the caller from raw values before `meta` is sanitized. Remaining findings are four minors: one PG test whose name claims an IntegrityError it never triggers, a tuple/set gap in the sanitiser walk, stale run numbers in PLAN.md's T11 closing note, and the sanitiser being absent from spec.md/ARCHITECTURE.md while the writer change got both.

## [MINOR] test_store_batch_survives_swallowed_integrity_error_in_one_outer_transaction never triggers an IntegrityError
`tests/test_postgres.py`

The test issues two sequential `storage.store_batch({"fp-outer": ...})` calls inside one outer `transaction.atomic()`. The second call goes through `_store_one`, which calls `_select_issue(fingerprint, alias)` first (storage.py) and finds the row the first call created — so `_create_issue()` is never reached and no `IntegrityError` is ever raised or swallowed. The test proves only that two batches inside an outer atomic block sum to one issue with count 2, which is what the PLAN's section-3 case 3 actually described; the test *name* and the implied savepoint coverage overclaim. The real savepoint proof lives in the two cases above it, which is why this is minor rather than a gap in the acceptance criteria.

**Fix:** Either rename to something honest (`test_store_batch_twice_in_one_outer_transaction_sums_into_one_issue`), or make it exercise the branch it claims: pre-create the issue and monkeypatch `storage._select_issue` to return `None` on its first call, so `_create_issue` hits the real unique-constraint violation and the outer transaction's survival is asserted end to end through `store_batch`.

## [MINOR] _sanitize_walk skips tuples and sets, so a BEFORE_SEND hook returning one can still abort the PostgreSQL write
`src/admin_errors/context.py`

`_sanitize_walk` (context.py:102-112) handles `str`, `dict` and `list`, and returns anything else verbatim. The stated purpose of moving the walk to `capture._build_and_store` (capture.py:439, DECISIONS p06-review_fix1/context) is that it runs after `_apply_before_send`, so *every* path into the stored payload is covered — but a host `BEFORE_SEND` hook that returns e.g. `payload["tags"] = ("a\x00b",)` produces a tuple that json-serialises into `jsonb` as a list with the NUL intact. PostgreSQL then rejects the insert and the whole capture is silently swallowed by the pipeline's `try/except BaseException`, the exact backend-divergent silent loss this phase exists to eliminate. Narrow (host-hook-only, and `extra` values are already repr-escaped by `safe_repr`), hence minor rather than major.

**Fix:** Extend the walk: `if isinstance(value, (list, tuple, set)): return [_sanitize_walk(item) for item in value]` (returning a list matches what JSON serialisation would produce anyway), and add a unit case in tests/test_context.py for a tuple carrying a NUL.

## [MINOR] T11's closing evidence no longer matches the delivered tree
`.autodev/phases/06-postgres-pass/PLAN.md`

T11 closes with "PostgreSQL suite 213 passed (no warnings, 3 consecutive runs), SQLite suite 206 passed/7 skipped". Re-running today's tree: `make test-pg` gives 216 passed with 1 warning, and `uv run pytest -q` gives 208 passed/8 skipped (matching TEST_OUTPUT.txt). The round-1 fixes added tests, and `test_connection_is_still_closed_when_the_loop_dies_outside_its_inner_try` now emits a permanent `PytestUnhandledThreadExceptionWarning` by design — DECISIONS p06-review_fix1/writer justifies not suppressing it, which I accept (the project has no `-W error` gate), but "no warnings" in T11 is now simply false. The phase's own closing evidence line is what a later phase or the release checklist will read as the baseline.

**Fix:** Update T11's numbers to 216 passed on PostgreSQL / 208 passed, 8 skipped on SQLite, and replace "no warnings" with a pointer to the one expected `PytestUnhandledThreadExceptionWarning` and its DECISIONS entry.

## [MINOR] The sanitiser is a new normalization of stored data with no spec or architecture entry
`docs/spec.md`

`sanitize_text`/`sanitize_payload` permanently rewrite what gets persisted — NUL and lone surrogates become U+FFFD in `Issue.title`, `Issue.exception_type`, `Issue.culprit`, `Issue.last_event` and `Event.payload`. That is observable in the admin UI and in anything a host reads out of those models, yet it appears only in CHANGELOG.md and DECISIONS.md. The writer's connection change from the same phase got both docs/spec.md:406-407 and ARCHITECTURE.md:356 updated, so the omission is inconsistent within the phase rather than a blanket "docs are Phase 10". Two spots are now slightly wrong: spec section 6.4's payload schema describes no such normalization, and ARCHITECTURE.md:96's pipeline line (`context.py (payload, scrub, truncate) → BEFORE_SEND`) places all of context.py's work *before* BEFORE_SEND, while `sanitize_payload` deliberately runs after it.

**Fix:** Add one bullet to spec section 6.4 (or the section 9 storage rules) stating that NUL and unpaired surrogates are replaced with U+FFFD before storage, since PostgreSQL's `text`/`jsonb` reject them, and amend ARCHITECTURE.md:96 to show the sanitise step after BEFORE_SEND (e.g. `… → BEFORE_SEND → size cap → sanitise → dispatch`).
