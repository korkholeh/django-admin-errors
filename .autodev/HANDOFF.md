# Handoff — django-admin-errors 0.1.0

Branch `autodev/spec-20260915-1213`, 12 commits, 10 phases, all committed. Written 2026-09-16.

## What was built

`django-admin-errors` is a zero-dependency reusable Django app that captures unhandled exceptions
and error-level log records into the host project's own database and shows them Sentry-style inside
the Django admin — grouped by fingerprint, deduplicated, with traceback, request context, occurrence
counts and 14/30-day trends. Capture runs on the host's thread and never raises; all database I/O
happens in a per-process background writer thread, and four independent limiters keep storage
bounded. The package is complete, tested on SQLite and PostgreSQL, built, and ready to publish —
nothing has been uploaded to PyPI.

## State — every check, with its result

All of the following were **run in this session**, from a clean repository root, and are the basis
for every claim in this file:

| Check | Command | Result |
|---|---|---|
| Unit gate (SQLite) | `uv run pytest -q` | **357 passed, 8 skipped** (the 8 are `DJANGO_DB=postgres`-only), exit 0 |
| PostgreSQL | `make test-pg` | **365 passed**, exit 0; container brought up and torn down cleanly |
| Coverage | `uv run pytest -q --cov=admin_errors --cov-report=term` | **91 %** (1929 statements, 147 missed) |
| Lint gate | `uv run ruff check . && uv run ruff format --check . && django check && makemigrations --check --dry-run` | all four green, exit 0 (`101 files already formatted`, `No changes detected in app 'admin_errors'`) |
| e2e | `make e2e-up && uv run --extra e2e pytest e2e -q && make e2e-down` | **25 passed, 1 deselected**, exit 0, on a freshly started server. The deselected case is `test_screenshots.py`, excluded by `-m 'not screenshots'` in `pyproject.toml` on purpose. |
| Build | `uv run python -m build && uv run twine check dist/*` | wheel + sdist built, both `PASSED` |
| Packaging | `uv run tox -e package` | exit 0, ending in `package_smoke: OK — templates, static files and the uk catalogue all resolve.` |
| Benchmark | `uv run python benchmarks/bench_capture.py` | exit 0, every budget met: no locals p50 1.348 ms (budget 2.0), with locals 1.682 ms (10.0), count-only 0.013 ms (0.3); `store_batch` 173,415 occ/s |

**Assumed, not verified in this session:**

- `uv run tox` (the full 11-cell Python × Django matrix). Only `tox -e package` was run here. CI
  runs the matrix; the last recorded full-matrix evidence is from Phase 10.
- The i18n round-trip (`makemessages -l uk` / `compilemessages`). Not run because it rewrites the
  committed `.po`/`.mo`. The shipped catalogue is asserted complete by a `.po`-parsing test in
  `tests/test_admin.py`, which is green.
- `make demo` / `make demo-pg` were not run directly; they share their migrate/seed/runserver path
  with `make e2e-up`, which was run and worked.
- Nothing has been published to PyPI, and no PyPI credentials were used or needed.

### The e2e warning in PROGRESS.md is stale

`.autodev/PROGRESS.md` records phase 10 as finishing with *"e2e still failing after 3 fix attempts
(3 failed, 22 passed)"*. That run was against a demo `runserver` process that had already served
several full e2e suites; the per-fingerprint `EVENT_SAMPLE_PER_HOUR` token buckets are process-global
and were drained, so later occurrences were legitimately stored count-only, with no payload for the
assertions to find. The last fix (`p10-e2e_fix3`, moving one test onto its own `/async-boom/`
fingerprint) landed *after* that recorded run. On a fresh server in this session the suite is green.
**The product bug count from those failures is zero.** The fragility is real but is an e2e harness
property, not a defect — see *Known gaps*.

## Decisions a human should confirm

These are product judgements, not implementation details. Each is defensible, each is reversible,
and each is the kind of thing an owner might decide differently. Full reasoning is in
`.autodev/DECISIONS.md` under the named step.

1. **Resolving an issue does not reset its notification throttle** (`p09-review_fix1/minor-rejected`,
   re-raised and rejected again as `p09-review_fix2/minor-rejected2`). An issue resolved and then
   hit again within `NOTIFY_THROTTLE_SECONDS` (one hour) shows the **Regressed** badge but sends no
   email. The argument for keeping it: an operator resolving an issue they are actively watching
   does not need to be emailed a minute later. The argument against: "I fixed it, it came back" is
   exactly the event most worth an email. One line in `notifications.py` changes it.
2. **`notified_at` is set before the send, and a failed send is not retried**
   (`architect/design`). Setting it after would double-send under concurrency; setting it before
   loses the notification if SMTP is down. The chosen trade is "at most once", on the grounds that
   the issue is still visible in the admin. An installation that treats these emails as the primary
   alerting channel would want the opposite.
3. **Request method and path are readable with `view_issue` alone**; only headers, cookies,
   GET/POST, the user block, Celery args and frame locals need `view_issue_context`
   (`architect/design`). A URL path can itself be sensitive (`/users/42/reset-token/abc…`). If your
   threat model says so, move the path behind the second permission.
4. **A `CAPTURE_LEVEL` below the root logger's level makes `ready()` lower the root logger**
   (`p03-review_fix1/apps`). Without it, a lower `CAPTURE_LEVEL` is silently unreachable — but the
   side effect is that those records also reach the host's other root handlers. Documented in the
   README and CHANGELOG; still a surprise waiting for someone.
5. **Django 4.2 is kept as the floor** although it reached upstream end of life in April 2026
   (spec §3, risk 10). It costs matrix slots and blocks newer APIs. Dropping it would simplify CI.
6. **Rate limiting is process-local, never shared** (`architect`, ADR 0006). Sampling and admission
   budgets multiply by worker count. The alternative puts a cache dependency on the error path,
   where the cache is most likely to be the thing that broke.
7. **`IDLE_CONNECTION_SECONDS` (60 s) and `RETRY_SLEEP_SECONDS` (0.1 s) are module constants in
   `writer.py`, not settings** (`p04-plan/writer`). Deliberately not part of the 40-key public
   settings contract. An operator who needs to tune them currently cannot.
8. **`demo/demo_app/views.py::reset_rate_limits`** — a `DEBUG`-gated, POST-only demo endpoint that
   calls `admin_errors.capture.reset_rate_limits()`, added to make e2e runs repeatable
   (`p10-e2e_fix1/fix`). `demo/` is never packaged, so it does not reach users, but it is a test hook
   living in an app directory and deserves a second pair of eyes.
9. **`pyproject.toml` declares `Development Status :: 3 - Alpha` at 0.1.0** (`p10/plan`). If you
   intend to publish this as production-ready, that classifier is the first thing to change.

## Known gaps

- **e2e sampling fragility.** Several e2e cases share the `/boom/` `ZeroDivisionError` fingerprint,
  and its `EVENT_SAMPLE_PER_HOUR` bucket is process-global. Counting the real hits across the suite
  (3+1+3+1+1 = 9) already exceeds the demo's capacity of 5 within a single fresh run, so any *new*
  case added on that fingerprint will flake, and a second run against the same server can fail cases
  that passed the first time. Mitigations in place: a session-scoped reset fixture in
  `e2e/conftest.py`, and the one test that needed determinism moved to its own `/async-boom/`
  fingerprint. The durable fix is a per-test-fresh server or one dedicated demo URL per case;
  neither was done. Start any red e2e triage with `make e2e-down && make e2e-up`.
- **`e2e/conftest.py` swallows its own reset failure.** `_reset_demo_rate_limits` wraps its POST in
  `except (URLError, OSError): pass`, so if the demo server predates the endpoint the fixture
  silently does nothing. Noted as a secondary fragility in `p10-e2e_fix3`, not fixed.
- **`deferred_not_authored` e2e cases.** Eleven cases are listed as deliberately not written, each
  with a reason, in `e2e/plans/*.yaml`. Worth a skim before anyone assumes the browser suite covers
  everything:
  - `admin-login`: `issue-list-and-detail` (superseded by the Phase 8 plan), `logout` (stock Django).
  - `admin-ui`: `storm-5000-under-one-second-in-a-browser` and `errors-cleanup-dry-run-report`
    (both covered at the HTTP/CLI layer), `dark-mode-visual-check` — **genuinely not assertable**;
    it is covered only by looking at the four screenshots in `docs/img/`. If dark mode breaks, no
    test will tell you.
  - `demo-app-surface`: `sensitive-form-submission-is-scrubbed-on-the-debug-page` — Django's own
    `SafeExceptionReporterFilter.is_active()` returns `settings.DEBUG is False`, so the raw debug
    page deliberately shows secrets when `DEBUG=True`, which the demo runs with. Our actual
    guarantee (the stored payload never contains them) *is* tested. Also
    `storm-and-unique-storm-in-a-browser` and `task-and-async-boom-in-a-browser`.
  - `manual-qa`: `non-superuser-permission-gating` (covered by `test_admin_ui.py` + unit tests).
  - `notifications-i18n`: `viewer-copy-as-text-gating` and `uk-locale-rendering` (both covered at
    the HTTP layer).
- **A label/behaviour mismatch on the changelist.** The summary card reads **Events last 24h**, but
  `admin.py::_summary_cards` sums the `IssueDailyCount` rows for today and yesterday *by UTC date* —
  a calendar-day span between roughly 24 and 48 hours, not a rolling window. Harmless, cheap to fix
  either way (change the query or change the label). `docs/user/triage-issues.md` documents the real
  behaviour; the code was not touched, because this step forbade it.
- **One phase-level warning in PROGRESS.md besides the stale e2e one:** phase 8's review round 2
  modified the working tree itself (the four `docs/img/*.png` screenshots), so those files landed in
  commit `56b4a5c` without a review pass over them. They are screenshots; look at them if you care.
- **Nothing needs credentials, hardware or a product answer to proceed.** Docker was available and
  used; the PostgreSQL path is verified end to end. Publishing is the only externally-gated step,
  and it was explicitly out of scope for this run.

## Open risks still live

From `.autodev/RISKS.md`, the rows whose mitigation is in place but whose underlying risk has not
gone away:

| # | Risk | Where it stands |
|---|---|---|
| 4 | SQLite write-lock contention on hosts that add a second writer | Mitigated structurally (no DB access on the request path, tiny transactions, chunked deletes, one retry then drop) and documented (WAL, `timeout`, dedicated alias). Untested under real production load. |
| 10 | Django 4.2 is upstream EOL but is the supported floor | Accepted and documented; it is a support-promise problem, not a code problem. |
| 13 | Email notifications: lost on SMTP failure, throttled after a resolve | Duplicates are solved by the conditional UPDATE, proved with two threads. The two accepted losses are items 1 and 2 under *Decisions a human should confirm*. |
| 14 | The e2e layer is the most fragile part of the repo | Real, and it bit this run three times. See *Known gaps*. |
| 18 | Unbounded growth despite the limiters | Four independent mechanisms, each tested in isolation with a frozen clock. The 6.4 GB theoretical worst case has never been exercised against a real database. |
| 1 | The UI does not actually support triage in practice | The §13 manual-QA script passes as a browser suite and the screenshots look right, but no real operator has used this against real production errors. |

Risks 2, 3, 5, 6, 7, 8, 9, 11, 12, 15, 16, 17, 19 and 20 are closed by shipped code and tests.

## Next steps, in order

1. **Look at the four screenshots in `docs/img/`** — light and dark, list and detail. They are the
   one acceptance criterion with no automated assertion behind it, and they are also what a
   prospective user sees first in the README.
2. **Decide the two notification questions** (items 1 and 2 above). They are the only places where
   the product behaves in a way a reasonable owner might call wrong, and both are small changes
   while the code is fresh.
3. **Harden the e2e sampling fragility** before adding any new browser case: give each case its own
   demo URL, or restart the demo server per test module. Until then, treat a red e2e run as
   "restart the server and re-run" before treating it as a bug.
4. **Run `uv run tox`** once on a machine with all four Python versions available, to confirm the
   matrix beyond the single interpreter used here.
5. **Then release, if that is the intent**: decide the `Development Status` classifier, tag, and
   `twine upload`. The artifacts in `dist/` are already built and `twine check`-clean.
