# Review — phase 6 round 1

**Verdict:** changes_requested

Phase 6 does what it set out to do: I independently confirmed the full suite green on the phase's own `postgres:16` container (213 passed), the two-thread same-fingerprint race and all three `assertNumQueries` budgets running for real on PostgreSQL, `grep -rn "select_for_update(" src/` empty, `make e2e-up` no-opping with exit 0, SQLite at 206 passed/7 skipped, ruff clean, and no container left behind. Every acceptance criterion maps to a test or a command I re-ran. The savepoint verdict is honest — the discipline held since Phase 3 — and the two new savepoint-isolation cases assert the one thing SQLite structurally cannot (outer transaction still usable after a swallowed IntegrityError). The writer-shutdown connection leak found in T7 is a genuine PG-only defect, correctly diagnosed and regression-tested. One major finding blocks approval: the new `sanitize_text`/`_sanitize_walk` fix covers dict values but not dict keys, so an exception captured on a request carrying a NUL in a query-parameter name is still silently dropped on PostgreSQL while storing fine on SQLite — I reproduced this end to end against the container (fp=None, 0 issues). The same call site also applies the walk before the `extra` merge and before `BEFORE_SEND`, leaving both unsanitized. Three minors (test-pg ignores pg-up's exit status and can fall through to an operator's own server; the shutdown close is not in a `finally`, so the leak it fixes can recur; the writer connection-lifecycle docs and T10's "no behaviour change" note are now stale) and one nit.

## [MAJOR] sanitize_text misses dict keys — a NUL in a query-param name still silently drops the whole capture on PostgreSQL
`src/admin_errors/context.py`

`_sanitize_walk` (context.py:105) rebuilds dicts as `{key: _sanitize_walk(item) ...}` — values are sanitized, keys are passed through verbatim. `build_request_block` puts untrusted names straight into `query`, `post`, `cookies` and `headers` keys, so a NUL in a parameter *name* reaches `jsonb` untouched. Reproduced on the phase's own container: `rf.get('/x/?bad%00key=1')` + `api.capture_exception(exc, request=request)` under `DJANGO_DB=postgres` returns `fp=None` with `Issue.objects.count()==0` (capture swallowed by the `try/except BaseException`), while the identical test passes on SQLite — exactly the backend-divergent silent loss this phase exists to eliminate, and remotely triggerable by any client. Two smaller holes in the same call site: `capture._build_and_store` merges host-supplied `extra` (capture.py:422) and runs `_apply_before_send` *after* `build_payload` has already walked the payload, so `extra` keys and anything BEFORE_SEND returns bypass the sanitiser entirely. Neither `tests/test_postgres.py::test_exception_message_with_nul_is_stored_on_postgresql` nor `test_build_payload_sanitizes_nul_recursively_through_nested_request_query` can catch this: both only assert on values.

**Fix:** In `_sanitize_walk`, sanitize keys too: `{sanitize_text(k) if isinstance(k, str) else k: _sanitize_walk(v) ...}`. Move the walk out of `build_payload` to the end of `_build_and_store` (after the `extra` merge, `_apply_before_send` and `_enforce_size`, before `_dispatch`) so every path into the payload is covered once. Add a PG case asserting a request with a NUL query-param *name* stores an issue, and a unit case for a NUL in an `extra` key.

## [MINOR] test-pg ignores pg-up failure and can run the suite against an operator's own PostgreSQL
`Makefile`

`test-pg` is `@$(MAKE) pg-up; DJANGO_DB=postgres ... pytest -q; st=$$?; $(MAKE) pg-down; exit $$st` — the `;` after `pg-up` discards its exit status. PLAN.md §Risks explicitly names the live hazard: port 5432 already held by the machine's Postgres.app. In that case `docker compose up -d --wait` fails on the port bind, pytest connects to the *operator's* server instead, and creates/drops `test_admin_errors_test` there — with the compose failure invisible in the output unless someone reads the scrollback.

**Fix:** Gate on success: `@$(MAKE) pg-up && { DJANGO_DB=postgres ... uv run pytest -q; st=$$?; $(MAKE) pg-down; exit $$st; }`.

## [MINOR] Shutdown connection close is not in a finally, so the leak it fixes can still happen
`src/admin_errors/writer.py`

The new `connections[conf.DATABASE].close()` (writer.py:201-211) sits after the `while True:` loop, not in a `finally`. Two statements per iteration run outside the loop's inner `try`: `assert self._queue is not None` (writer.py:130) and `timeout = self._time_to_next_flush(...)` (writer.py:131, which reads `conf.FLUSH_INTERVAL_SECONDS` — a bad host setting makes it a `TypeError`). Either one kills the thread by the non-`break` path and skips the close, reproducing the exact leaked server-side session and `DROP DATABASE` abort documented in DECISIONS.md p06-implement/T7.

**Fix:** Wrap the loop: `try: while True: ... finally: try: connections[conf.DATABASE].close() except Exception as exc: stats.last_error = type(exc).__name__`.

## [MINOR] T10's "T7 changed no behaviour" is inaccurate; the writer's connection-lifecycle docs are now incomplete
`.autodev/ARCHITECTURE.md`

PLAN.md T10 closes with "T7 changed no behaviour, so ARCHITECTURE.md is untouched", but T7 did change runtime behaviour: the writer thread now closes its database connection unconditionally when it stops, not only after `IDLE_CONNECTION_SECONDS`. ARCHITECTURE.md:356 still reads "close the connection after 60 s idle rather than holding one forever" and docs/spec.md:406-407 states the same idle-only rule — both now describe less than the code does. The T10 rationale also justifies leaving ARCHITECTURE.md alone via the `context.py` "scrub" row, which covers the sanitiser but says nothing about the writer.

**Fix:** Extend ARCHITECTURE.md:356 (and spec.md:406-407's connection-hygiene bullet) with "…and once the writer thread stops", and correct the T10 note.

## [NIT] Module docstring cites the wrong spec section
`tests/test_postgres.py`

The docstring opens with "(Phase 6, spec section 11.3)", but docs/spec.md §11.3 is "Dedicated database alias" — unrelated to PostgreSQL-only coverage.

**Fix:** Drop the citation or point it at the section that actually covers PostgreSQL support.
