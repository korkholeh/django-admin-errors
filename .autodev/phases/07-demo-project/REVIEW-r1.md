# Review — phase 7 round 1

**Verdict:** approve

Phase 7 hits its goal and the gate is genuinely green — I re-ran everything rather than trusting PLAN.md. `uv run pytest -q` → 238 passed, 8 skipped (skips are the `postgres_only` guards, expected on SQLite); `make lint` exit 0 (ruff check, format --check, django check, makemigrations --check); `uv run python demo/manage.py check` → 0 issues. I ran the whole e2e cycle myself: `make e2e-up` migrated, seeded 40 issues and answered on :8000; `uv run --extra e2e pytest e2e -q` → 3 passed, so the real-browser login case ran rather than skipped; a second `make e2e-up` printed "server already answering" and exited 0 with exactly one listener on 8000 (`lsof` showed one row, uv wrapper pid 95209 → python child 95212); `make e2e-down` killed both, removed `.autodev/e2e-server.pid` and left port 8000 free. I built the sdist to /tmp: it contains 22 `tests/` files and neither `demo/` nor `tests/test_demo.py`, so the packaging guard works. Every acceptance criterion maps to a test that can fail: the URL-status sweep, both collapse cases, the `/404/` negative, the storm sync-exactness case (1 issue, count 25, 5 events, 1 daily row), the 5000-storm timing case under `TRANSPORT="thread"` + `transaction=True` + `api.flush()`, `demo_seed`'s 40/30/superuser/idempotency/admission cases. The `/sensitive/` test is not vacuous — `CAPTURE_LOCALS` defaults to `True` and `context.py` scrubs `AUTH*` META keys explicitly, so all three secret literals are real probes. No `[~]` tasks, no environment excuses, all 12 PLAN tasks checked and matched by the diff, and the one deliberate assertion relaxation (`0 < issue.count <= 5000` instead of `== 5000`) is honestly recorded in DECISIONS with the root cause, not a quiet weakening. No blockers or majors. What is left is small: `demo_seed` can only produce 64 distinct issues yet reports whatever `--issues` said (I reproduced `--issues 100` → 64), the README still promises the storm's exact +5000 that the queue-overflow behaviour contradicts, and `e2e-up`'s ready-poll runs even after a failed migrate/seed and then blames the server.

## [MINOR] `--issues N` silently caps at 64 but reports N
`demo/demo_app/management/commands/demo_seed.py`

The seed varies only `exc_cls = _EXCEPTIONS[i % 8]`, `culprit_fn = _CULPRITS[(i // 8) % 8]`, `verb = _VERBS[i % 8]`, `noun = _NOUNS[(i * 5) % 8]`. For `i` and `i + 64` all four are identical (`(i+64)//8 % 8 == i//8 % 8`, `5*(i+64) % 8 == 5i % 8`), and `normalize_message` strips digits anyway, so fingerprints collide. `_seed_one` then re-`get()`s the existing issue and rewrites it. Proven by running it: `uv run python demo/manage.py demo_seed --issues 100 --days 30 --reset` printed `seeded 100 issues`, `Issue.objects.count()` was 64. The command reports `n_issues` from the options rather than what it created, so the operator is told a false number. The phase's own acceptance criterion (40) and both tests (40, 60) stay inside the budget, which is why the gate is green; a screenshot run at `--issues 100` in Phase 8/10 would silently get 64.

**Fix:** Report the real number (`Issue.objects.using(alias).count()` delta) in the success line, and either widen the pair space (e.g. append a unique non-numeric token derived from `i` to the message, since digits are normalized away) or raise `CommandError` when `--issues` exceeds `len(_EXCEPTIONS) * len(_CULPRITS)`.

## [MINOR] Manual QA script promises `/storm/?n=5000` → "issue count +5000", which the writer cannot guarantee
`README.md`

The README "Try it" section (and spec §13) tells the reader to expect `issue count +5000`. But `QUEUE_MAXSIZE` defaults to 1000 and spec §8 says overflow drops the newest item; `tests/test_demo.py::test_storm_of_5000_returns_under_one_second` accordingly asserts only `0 < issue.count <= 5000`, and DECISIONS `[p07-implement/T8]` records that the exact-count assertion failed and was relaxed for exactly this reason. The relaxation is defensible and logged, but the user-facing doc was not brought along: a reader following the QA script sees a count well below 5000 and concludes aggregation is broken.

**Fix:** Reword the README line to what the product actually does, e.g. "response under 1 s, one issue, at most `EVENT_SAMPLE_PER_HOUR` stored events; the occurrence count grows by up to `n` — a tight burst overflows the writer queue (`QUEUE_MAXSIZE`) and the dropped occurrences show up as `dropped_queue_full` in `errors_stats`." Same correction is worth making in spec §13 so the two stop disagreeing.

## [MINOR] `e2e-up` runs the 45 s ready-poll even when migrate/seed/playwright failed, masking the real error
`Makefile`

The recipe is `... && (runserver & echo $$! > PIDFILE) && \\\n\ti=0; \\\n\twhile ...`. The `;` after `i=0` ends the `&&` chain, so a failure in `migrate`, `demo_seed` or `playwright install` does not short-circuit: the while loop still polls the ready URL 45 times, sleeping a second each, and the run ends with `e2e: server never became ready on ...` — which points at the server rather than the actual failure, and costs 45 s of wall clock on every broken run. Exit status is still 1, so the orchestrator is not misled, only the operator.

**Fix:** Make the chain fail fast: `... && (runserver & echo $$! > $(PIDFILE)) || exit 1;` before `i=0`, or wrap the setup steps in `{ ...; } || { echo "e2e: setup failed"; exit 1; }`.

## [NIT] `make demo` seeds without `--reset` while `make e2e-up` uses it
`Makefile`

`e2e-up` was changed to `demo_seed --issues 40 --days 30 --reset` precisely so the browser surface is identical every run (DECISIONS p07-plan). `demo`/`demo-pg` kept the non-reset form, so repeated `make demo` re-captures the same 40 fingerprints, accumulates extra `Event` rows and rewrites counts from a fresh random walk — the "40 issues over 30 days" the README describes drifts run over run.

**Fix:** Either add `--reset` to the `demo`/`demo-pg` recipes, or state in the README that repeated `make demo` accumulates and that `demo_seed --reset` restores a clean surface.

## [NIT] Demo views and tasks carry no type hints
`demo/demo_app/views.py`

CLAUDE.md requires "Type hints on all public functions". `demo_seed.py` complies, `views.py` does not (`def boom(request):`, `def storm(request):`, `async def async_boom(request):`, …) and `tasks.py`'s `fail_task` alias is untyped. ruff is not configured to catch this, so it slipped through lint. The demo is the file a prospective user reads first, so it is the worst place to model a different convention.

**Fix:** Annotate the views (`def boom(request: HttpRequest) -> HttpResponse:` etc.); `_clamp_n` already takes `default: int` so only the `request` parameter and the return types are missing.

## [NIT] The sensitive form has no `token` field, though spec/README describe a password/token form
`demo/demo_app/templates/demo_app/sensitive_form.html`

Spec §13 and the README table describe `/sensitive/` as a "POST form with a password/token". The template posts only `username` and `password`; `DEMO_TOKEN` exists solely as a view local covered by `@sensitive_variables("token")`. Both halves of the scrubbing story are actually exercised (POST parameter + traceback local + the injected `Authorization` header, all asserted absent in `test_sensitive_view_payload_contains_no_secrets`), so this is presentation only — a human doing the manual QA walk cannot see the token half.

**Fix:** Add a `token` input to the form and read it into the local that `@sensitive_variables` protects, so the POST-parameter and the local-variable scrubbing paths are both visible from the browser.
