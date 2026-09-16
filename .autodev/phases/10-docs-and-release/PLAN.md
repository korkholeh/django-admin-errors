# Phase 10 — Documentation, release readiness and final verification

Goal: ship-ready 0.1.0 — one migration, a complete README with screenshots, operator docs under
`docs/user/`, a wheel that installs and migrates in a clean venv with its templates/static/`uk`
catalogue intact, and the spec §13 manual QA script passing on SQLite and PostgreSQL.

## Context

What already exists (verified in this session, not assumed):

| Area | State |
|---|---|
| Package | `src/admin_errors/` complete: `conf.py` (39 keys in `DEFAULTS`), `models.py`, `fingerprint.py`, `context.py`, `capture.py`, `storage.py`, `writer.py`, `retention.py`, `routers.py`, `notifications.py`, `textformat.py`, `checks.py`, `admin.py`, `api.py`, `handlers.py`, `middleware.py`, `signals.py`, `tasks.py`, `integrations/`, `templates/`, `static/`, `templatetags/`, `management/commands/{errors_cleanup,errors_stats,errors_test}.py`, `locale/uk/LC_MESSAGES/django.{po,mo}` (both tracked by git). |
| Migrations | `src/admin_errors/migrations/0001_initial.py` only — **already exactly one**. `makemigrations --check --dry-run` is part of the lint gate and is currently green. |
| Version | `src/admin_errors/__init__.py` → `__version__ = "0.1.0.dev0"`. `CHANGELOG.md` has one big `## [Unreleased]` section (211 lines), no `0.1.0` section. |
| README | 107 lines: title, status ("pre-release"), compatibility table, a stub *Install* section that defers to the spec, a *Development* command table, a *Try it* section (demo URL map + the §13 QA script as prose), *License*. Missing: screenshots, real install steps, the ASCII pipeline diagram, the settings table, permissions and groups, retention/Celery-beat/alias/SQLite tips, notifications and signals, Celery/ASGI/gunicorn notes, the storage bound and benchmark numbers, the FAQ, non-goals, contributing. |
| `docs/` | `spec.md`, `dev/adr/0001..0008`, `img/{issue-list,issue-detail}-{light,dark}.png` (4 real screenshots, committed in phase 8). **`docs/user/` does not exist** — phase 9 explicitly deferred it here. |
| Tests | 342 passed / 8 skipped on SQLite (skips are the `postgres_only` guards). Coverage **91 %** (`TOTAL 1929 stmts, 147 miss, 91%`) — already over the 90 % gate. `grep -rn "pragma: no cover" src/admin_errors/` → **no hits at all**. |
| Packaging | `pyproject.toml`: `dependencies = ["Django>=4.2"]` only; extras `e2e`/`celery`/`postgres`; hatch `artifacts` list covers `locale/**/*.mo`, `static/**`, `templates/**`. `tox.ini` already has a `package` env (build → twine check → `uv venv` → install from `--find-links dist` → `django check` + `migrate` with `tests/package_settings.py`) and CI already has a `package` job. |
| e2e | `e2e/` with `conftest.py`, `plans/*.plan.yaml`, `test_admin_login.py`, `test_demo_surface.py`, `test_admin_ui.py`, `test_notifications_i18n.py`, `test_screenshots.py` (marker `screenshots`, deselected by default), `README.md`, `RESULTS.md`. `make e2e-up` / `e2e-down` work and are idempotent. |
| Benchmarks | `benchmarks/bench_capture.py` prints a p50/p95/budget table and gates on `BUDGETS` (`capture, no locals` ≤ 2.0 ms, `with locals` ≤ 10.0 ms, `count-only` ≤ 0.3 ms). Its docstring already says the produced numbers belong in `CHANGELOG.md`. |

What this phase changes: documentation (`README.md`, new `docs/user/`), release metadata
(`__version__`, `CHANGELOG.md`), one new test module (`tests/test_docs.py`), one new clean-venv smoke
script wired into the `package` tox env (`tests/package_smoke.py`), one new e2e spec that encodes the
§13 manual QA script (`e2e/test_manual_qa.py` + `e2e/plans/manual-qa.plan.yaml`), and two recorded
result files under this phase directory. **No product code changes are planned.** If a documentation
task uncovers a product defect, it is fixed in the product (rule 9), not papered over in prose.

Key files to touch: `README.md`, `CHANGELOG.md`, `src/admin_errors/__init__.py`, `docs/user/*.md`,
`tests/test_docs.py`, `tests/package_smoke.py`, `tox.ini` (`[testenv:package]`),
`e2e/test_manual_qa.py`, `e2e/plans/manual-qa.plan.yaml`,
`.autodev/phases/10-docs-and-release/{BENCH.md,QA-RESULTS.md}`.

## Design

### Nothing in the architecture changes

`.autodev/ARCHITECTURE.md` names the README as the home of the FAQ (line 297), the Celery-beat
schedule (line 412), the SQLite/queued-email guidance (line 358) and the demo/screenshot surface
(line 416). This phase fills exactly those slots. There is **no deviation** from ARCHITECTURE.md, and
no new module, model, setting or public function is introduced. The only non-doc artefacts are test
assets (`tests/test_docs.py`, `tests/package_smoke.py`, `e2e/test_manual_qa.py`), which the component
table does not enumerate.

### Migration "squash" is a verification, not a rewrite

`src/admin_errors/migrations/` already contains exactly `0001_initial.py`, so the deliverable
"squash to exactly one initial migration" is already satisfied by construction (every phase kept the
lint gate's `makemigrations --check --dry-run` green). Squashing a single migration would replace a
reviewed file with an equivalent one and re-open risk #19 for no benefit. This phase therefore
*pins* the invariant with a test (`test_exactly_one_migration_ships`) and re-runs the check, rather
than rewriting the file.

### Documentation structure

`README.md` follows spec §17's twelve-item outline, in that order, with the existing *Compatibility*,
*Development* and *Try it* sections retained and folded into items 1, 12 and 12 respectively:

1. What it is / when to use it instead of Sentry, and when not to (existing intro, kept).
2. Screenshots — all four `docs/img/*.png`, light and dark, list and detail, with alt text.
3. Install — `pip install`, `INSTALLED_APPS`, `migrate`, optional `RequestContextMiddleware`,
   `LOGGING` snippet, `ADMINS` + `EMAIL_BACKEND`, and `manage.py errors_test` as the "did it work?"
   step, plus the compatibility table.
4. How it works — one fenced ASCII diagram: `raise / logger.error → capture (guards, scrub,
   fingerprint, BEFORE_SEND) → queue → writer thread → aggregate → Issue/Event/IssueDailyCount →
   admin`, with the four limiters annotated on the edges they cut.
5. Settings reference — the full table, one row per key in `conf.DEFAULTS`, copied from spec §5
   (which matches `DEFAULTS` key-for-key: 39 keys).
6. Permissions and groups — `view_issue` / `change_issue` / `delete_issue` / `view_issue_context`,
   with a "triage" group recipe and the explicit warning that `view_issue_context` is what exposes
   headers, cookies, POST data and frame locals (ADR 0007).
7. Retention — the four mechanisms (ADR 0006), `errors_cleanup` (`--dry-run`, `--vacuum`), a Celery
   beat schedule snippet for `admin_errors.tasks.cleanup`, the dedicated DB alias recipe
   (`DATABASE`, the router, `migrate --database=`), and SQLite tips: WAL, `OPTIONS={"timeout": 20}`,
   `auto_vacuum=2` + `SQLITE_VACUUM="incremental"`.
8. Notifications and signals — `NOTIFY_*`, the throttle, `issue_created` / `issue_regressed` /
   `issue_status_changed` as the sanctioned extension point (risk #15), a receiver example, and
   *Replacing `mail_admins`*: why Django's `AdminEmailHandler` mails on every occurrence, how to drop
   it from `LOGGING`, and that check `W003` flags the overlap. Recommends a queued email backend.
9. Celery / ASGI / gunicorn — eager vs worker, `--preload` and the pid-change guard (risk #11),
   autoreloader, `flush()` on shutdown, async views and the sync-capable middleware.
10. Storage bound and benchmarks — spec §9.4's arithmetic plus the real numbers from a
    `benchmarks/bench_capture.py` run on this machine, with the machine named.
11. FAQ — each silent failure mapped to its instrument, at minimum: nothing appears
    (`propagate: False` → `W002`; `DEBUG`/`CAPTURE_IN_DEBUG`; `CAPTURE_LEVEL` above what the host
    logs; `ENABLED=False`) → `errors_test`; too many issues (`NEW_ISSUES_PER_MINUTE`, `MAX_ISSUES`,
    `fingerprint=` override) → `errors_stats`; events missing while the count grows
    (`EVENT_SAMPLE_PER_HOUR`, `EVENTS_PER_ISSUE`, queue overflow) → `errors_stats`; PII concerns →
    delegated scrubbing, `view_issue_context`, "settings and environment are never stored";
    migrating from `django-db-log`-style apps; `database is locked` on SQLite → WAL + alias.
12. Non-goals and roadmap; contributing (`make test`, `make test-pg`, `make demo`, the existing
    *Development* table and *Try it* section).

`docs/user/` follows `.autodev/guides/user-docs.md`, task-shaped, in the operator's vocabulary
(*Errors*, *issue*, *Resolve*, not `Issue.objects`), with only the pages this project has something
true to say in:

| File | Contents |
|---|---|
| `README.md` | Index: what the *Errors* section is, who it is for, where to start. |
| `getting-started.md` | Install into an existing project and see the first issue in the admin, end to end, ending with `manage.py errors_test` and what you should now see. |
| `triage-issues.md` | Read the issue list (cards, sparkline, filters, search), open an issue, read the traceback / request / occurrences, Resolve / Ignore / Reopen, what *Regressed* means, and the "Copy as text" button. |
| `retention.md` | How much the app stores, the four limiters in plain words, running `errors_cleanup` (with `--dry-run` first), scheduling it, and `errors_stats`. |
| `notifications.md` | Turning email on, who receives it, what the throttle does, turning it off, and the `mail_admins` overlap warning. |
| `troubleshooting.md` | "It did not work" → ordered checks, each pointing at a command or a system check ID; links to the README FAQ rather than duplicating it. |

The FAQ is kept in one place — `README.md`, because spec §17 item 11 requires it there — and
`docs/user/troubleshooting.md` links to it. Two copies would drift.

### Test design (`.autodev/guides/case-taxonomy.md`)

The phase's own risk is that the docs assert things the code does not do, or the release metadata is
half-bumped. Both are pinned by executable checks, not by review.

`tests/test_docs.py` (new, pure-file tests, no DB):

| Case | Proves |
|---|---|
| `test_readme_settings_table_covers_every_setting` | The set of keys in the README's settings table equals `conf.DEFAULTS` exactly — **both directions**, so a new key without a row and a documented-but-nonexistent key both fail. Parses the markdown table's first column, stripping backticks. |
| `test_readme_settings_table_defaults_match_conf` | Each row's *Default* cell matches `repr`-equivalent of `DEFAULTS[key]` (normalised: `True`/`False`/`None`, lists, strings in backticks), so a copy-pasted stale default fails. |
| `test_readme_embeds_every_committed_screenshot` | Every file in `docs/img/*.png` is referenced by the README, and every image the README references exists. |
| `test_readme_has_the_spec_17_sections` | The README contains the outline's required headings (screenshots, install, how it works, settings, permissions, retention, notifications, deployment, storage bound, FAQ, non-goals, contributing) and at least one fenced ASCII-diagram block under *How it works*. |
| `test_readme_faq_maps_each_silent_failure_to_an_instrument` | The FAQ mentions `propagate`, `DEBUG`, `CAPTURE_LEVEL`, `errors_test`, `errors_stats`, `NEW_ISSUES_PER_MINUTE`, `EVENT_SAMPLE_PER_HOUR`, `view_issue_context` — the risk-#12 and risk-#1 instruments. |
| `test_changelog_has_a_released_0_1_0_section` | `CHANGELOG.md` has `## [0.1.0]` with a date, and no content left under `## [Unreleased]` other than the placeholder. |
| `test_changelog_0_1_0_records_benchmark_numbers` | The `0.1.0` section contains the three benchmark row labels and at least one millisecond figure each (spec §18). |
| `test_version_matches_changelog` | `admin_errors.__version__ == "0.1.0"` and equals the newest CHANGELOG release heading. |
| `test_django_is_the_only_runtime_dependency` | `tomllib`-parsed `project.dependencies` is exactly one requirement whose name is `Django`; optional-dependency groups are ignored. Also asserts the installed distribution's metadata (`importlib.metadata.requires`) has no non-extra requirement besides Django. |
| `test_exactly_one_migration_ships` | `src/admin_errors/migrations/` contains exactly one `\d{4}_*.py` file, named `0001_initial.py`. |
| `test_no_pragma_no_cover_on_capture_or_storage_paths` | `# pragma: no cover` appears nowhere in `capture.py`, `context.py`, `storage.py`, `writer.py`, `retention.py` (spec §18 / risk #16). Currently vacuously true — the test is what keeps it true. |
| `test_user_docs_index_links_resolve` | Every relative link in `docs/user/*.md` points at a file that exists (catches a renamed page). |

`tests/package_smoke.py` (new, run *by the clean venv's* Python inside the `package` tox env, not by
pytest — `testpaths = ["tests"]` collects `tests/`, so the file is named without a `test_` prefix and
has no importable test functions): configures a minimal Django with `admin_errors` installed, then
asserts, against the *installed* package:

- `django.template.loader.get_template("admin_errors/...")` resolves for the main admin templates;
- `admin_errors/admin_errors.css` and `.js` are found by `django.contrib.staticfiles.finders.find`;
- `locale/uk/LC_MESSAGES/django.mo` exists inside the installed package and
  `translation.activate("uk")` yields a translated string for a known `msgid`.

It exits non-zero with a readable message on failure. This is what turns risk #17 from "the wheel
imports" into "the wheel renders".

`e2e/test_manual_qa.py` + `e2e/plans/manual-qa.plan.yaml` (new): the spec §13 script as a durable
browser/HTTP spec against the live demo, so the acceptance criterion is re-runnable rather than a
one-night claim. Cases, in order, each enforcing its own precondition (the p09-e2e lesson: a
long-lived demo server outlives one run, so preconditions are *enforced*, never assumed — a case that
needs a fresh issue deletes any pre-existing one through the admin's own `delete_selected` action
first):

| Case | Oracle |
|---|---|
| `test_three_boom_hits_make_one_issue_with_count_three` | After deleting the `demo_app.views.boom` issue, three `/boom/` hits → exactly one issue in the list for that culprit, occurrence count 3. |
| `test_boom_1_and_boom_2_group_into_one_issue` | `/boom/1/` + `/boom/2/` → one `ValueError` issue, count 2 (message normalisation, risk #7). |
| `test_storm_is_fast_and_stores_few_events` | `/storm/?n=5000` responds in < 1 s (measured server-side by the view's own elapsed time *and* client-side wall clock) and the issue's detail page shows ≤ `EVENT_SAMPLE_PER_HOUR` stored occurrences. |
| `test_resolve_then_rehit_shows_regressed_badge` | Resolve in the admin, hit `/boom/` again → *Regressed* badge on the detail page. No email assertion: the demo's shipped `NOTIFY_THROTTLE_SECONDS=3600` plus resolve-does-not-reset-`notified_at` (p09-review_fix2/minor-rejected2) makes the regression email unobservable end-to-end when this case reuses the same-run `/boom/` issue; the email path is covered at unit level by `tests/test_notifications.py::test_regression_sends_one_email`. |
| `test_errors_cleanup_dry_run_prints_a_report` | `manage.py errors_cleanup --dry-run` in the demo exits 0, prints a report naming each retention rule, and deletes nothing (row counts unchanged before/after). |
| `test_dark_mode_is_readable` | With `prefers-color-scheme: dark`, the issue list and detail pages render and the status badge / sparkline foreground colours resolve to non-transparent values distinct from the page background (the phase-8 contrast fix stays fixed). |

Sizing: 12 doc/metadata cases + 3 package-smoke assertions + 6 e2e cases — inside the guide's
"prefer a small number of cases that each prove something the spec claims". Deferred as
`deferred_not_authored`: input/boundary, concurrency, accessibility and i18n cases — all owned by
earlier phases' suites, which stay green.

### Error handling

No runtime code paths are added, so there is no new failure mode in the product. The new checks fail
loudly and locally: `tests/test_docs.py` fails the gate, `tests/package_smoke.py` exits non-zero and
fails `tox -e package`, and the e2e spec fails the e2e run. The QA passes are recorded in
`.autodev/phases/10-docs-and-release/QA-RESULTS.md` with the exact commands and their output, so a
claim about the environment is never made without its proof (rule 11).

## Tasks

- [x] **T1: Release/doc invariant tests (red first).** New `tests/test_docs.py` with the twelve cases
  in the table above, plus a `repo_root`-style path helper reused from `tests/conftest.py`. Expected
  state after this task: the migration, pragma and dependency cases pass; the README, CHANGELOG,
  version and `docs/user/` cases fail. Do not touch docs yet — a red test that later goes green is
  the proof the doc tasks did something.
- [x] **T2: Benchmark run and record.** `uv run python benchmarks/bench_capture.py`, capture the full
  table verbatim into `.autodev/phases/10-docs-and-release/BENCH.md` together with the machine
  description (`uname -a`, Python version, SQLite version) and the exit code. If a budget is missed,
  fix the product or record the regression as a decision — never pass `--no-gate` to make it look
  green. Files: `BENCH.md` only.
- [x] **T3: README items 1–4.** Rewrite `README.md`'s head: what it is / when to use it (keep the
  existing intro), *Screenshots* embedding all four `docs/img/*.png`, real *Install* steps
  (`INSTALLED_APPS`, `migrate`, optional middleware, `LOGGING` snippet, `ADMINS`/`EMAIL_BACKEND`,
  `errors_test`), the compatibility table, and *How it works* with the fenced ASCII pipeline diagram.
  Verifies against: `test_readme_embeds_every_committed_screenshot`,
  `test_readme_has_the_spec_17_sections`.
- [x] **T4: README items 5–6.** Settings reference table (one row per `conf.DEFAULTS` key, defaults
  and meanings from spec §5) and *Permissions and groups* (the four permissions, the triage-group
  recipe, the `view_issue_context` warning). Verifies against:
  `test_readme_settings_table_covers_every_setting`,
  `test_readme_settings_table_defaults_match_conf`.
- [x] **T5: README items 7–9.** Retention and `errors_cleanup`, the Celery beat snippet, the
  dedicated-alias recipe, SQLite tips (WAL, `timeout`, `auto_vacuum` + `SQLITE_VACUUM`);
  notifications and signals with the receiver example and *Replacing `mail_admins`* (incl. `W003`);
  Celery / ASGI / gunicorn `--preload` notes. Every command and setting name in these sections is
  checked against the code before it is written (grep `retention.py`, `tasks.py`, `routers.py`,
  `notifications.py`, `checks.py`), not recalled.
- [x] **T6: README items 10–12.** Storage bound (spec §9.4 arithmetic) with the T2 benchmark numbers;
  the FAQ; non-goals and roadmap; contributing, folding in the existing *Development* table and
  *Try it* section; update the *Status* line away from "pre-release `0.1.0.dev0`". Verifies against:
  `test_readme_faq_maps_each_silent_failure_to_an_instrument`, `test_readme_has_the_spec_17_sections`.
- [x] **T7: `docs/user/`.** The six pages from the Design table, task-shaped, each ending with what
  the operator should now see; labels taken from the actual templates/admin code, never guessed.
  Verifies against: `test_user_docs_index_links_resolve`.
- [x] **T8: Release metadata.** `__version__ = "0.1.0"`; promote `CHANGELOG.md`'s `## [Unreleased]`
  body into `## [0.1.0] — 2026-09-16`, add the T2 benchmark numbers and a user-language summary line,
  and leave an empty `## [Unreleased]` above it. Verifies against:
  `test_changelog_has_a_released_0_1_0_section`, `test_changelog_0_1_0_records_benchmark_numbers`,
  `test_version_matches_changelog`.
- [x] **T9: Migration invariant.** Confirm `src/admin_errors/migrations/` holds exactly
  `0001_initial.py` (no squash needed — see Design) and run
  `uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings`.
  Record the command and output in `QA-RESULTS.md`. Verifies against:
  `test_exactly_one_migration_ships` and the lint gate.
- [x] **T10: Clean-venv wheel proof.** New `tests/package_smoke.py` (templates resolve, static files
  found, `uk` catalogue present and active); wire it as the last command of `[testenv:package]` in
  `tox.ini`, run by `.tox/package-install/bin/python`. Then `uv run tox -e package` must exit 0.
  Files: `tests/package_smoke.py`, `tox.ini`. (CI's `package` job already calls this env, so no
  workflow change is needed — confirm that rather than editing `ci.yml`.)
- [x] **T11: §13 manual QA as an e2e spec, on SQLite.** `e2e/plans/manual-qa.plan.yaml` (oracles
  traced to spec §13 / §18) and `e2e/test_manual_qa.py` with the six cases, written and green.
  **Handoff (session 2): code is done, the recorded QA-RESULTS.md write-up is not.**
  `e2e/test_manual_qa.py` exists with all 6 cases (`test_three_boom_hits_make_one_issue_with_count_three`,
  `test_boom_1_and_boom_2_group_into_one_issue`, `test_storm_is_fast_and_stores_few_events`,
  `test_resolve_then_rehit_shows_regressed_badge`, `test_errors_cleanup_dry_run_prints_a_report`,
  `test_dark_mode_is_readable`). Verified green: `make e2e-down && make e2e-up` (fresh demo server)
  then `uv run --extra e2e pytest e2e -q` → **25 passed, 1 deselected** (the whole `e2e/` suite,
  not just the new file). `make e2e-down` run afterwards; `lsof -nP -iTCP:8000 -sTCP:LISTEN` and
  `.autodev/e2e-server.pid` both confirm nothing is listening. Two real bugs were found and fixed
  in the test (not the product — the rate limiter is working as designed), logged in
  `.autodev/DECISIONS.md` under `p10-implement`:
  1. `_delete_existing_issue` now retries the bulk-delete and waits past one
     `FLUSH_INTERVAL_SECONDS` before trusting "cleared" — a delayed writer-thread flush from an
     *earlier* test's hit on the same shared fingerprint (`/boom/`'s `ZeroDivisionError`, also hit
     by `test_admin_ui.py` and `test_demo_surface.py`) could otherwise land right after the delete
     and silently reappear.
  2. `test_resolve_then_rehit_shows_regressed_badge` no longer deletes+recreates `/boom/`'s
     issue a *second* time within the same file — it reuses the issue
     `test_three_boom_hits_make_one_issue_with_count_three` just created (same file, runs first).
     A second recreation, once the fingerprint's process-wide `EVENT_SAMPLE_PER_HOUR` (demo: 5)
     budget was already spent by earlier tests, left the recreated issue with **no stored
     `last_event`**, which broke `test_notifications_i18n.py::test_copy_as_text_flips_to_copied`
     downstream (that test asserts `#ae-traceback-text` renders, which needs a payload). A
     `/keyerror/<unique-key>/` route was tried first to dodge the shared budget entirely, but was
     rejected: proven empirically (`e2e/plans/manual-qa.plan.yaml`'s case doc has the repro) that
     distinct keys on that route still collapse into **one** shared fingerprint — its
     title/message comes from the `django.request` log record (`"Internal Server Error: <path>"`),
     not from the key — so it gave no real isolation, only a different flake.
  **Known remaining fragility (not fixed, documented instead):** re-running the full `e2e/` suite
  *twice in a row* against the same **not-restarted** demo server can still exhaust `/boom/`'s
  shared `EVENT_SAMPLE_PER_HOUR` budget across the two runs combined (proven: first run 25/25
  green, immediate second run without `make e2e-down`/`up` in between →
  `test_copy_as_text_flips_to_copied` fails the same way). A single `make e2e-up` (fresh) →
  `pytest e2e` → `make e2e-down` cycle — which is what T13's gate and CI actually do — is not
  affected; this only bites iterative manual re-runs against a server left up between them. Treat
  it as a documented limitation, not a bug to chase further, unless a future session sees it in the
  single-run gate itself.
  **Session 3 completed the handoff:** `QA-RESULTS.md` now records T9, T11 (SQLite), T12
  (PostgreSQL) and T13 (final gate + teardown) in full — see those tasks below.
- [x] **T12: PostgreSQL pass.** `make pg-up`; full suite with `DJANGO_DB=postgres` → 362 passed.
  §13 QA script against a `DEMO_DB=postgres` demo server (started by hand: migrate, seed --reset,
  backgrounded runserver) — the T11 e2e spec ran unchanged, 25 passed / 1 deselected, identical to
  SQLite; `errors_cleanup --dry-run` printed a report and changed nothing. Recorded in
  `QA-RESULTS.md`; `make pg-down` run, teardown proven clean. See `.autodev/DECISIONS.md` p10/T12.
- [x] **T13: Final gate and teardown.** Ran, in order: ruff check + format check, both `django`
  checks, `uv run pytest -q` (354 passed, 8 skipped), `--cov-fail-under=90` (91.03%),
  `uv run tox -e package` (green, `package_smoke` OK), a fresh `make e2e-up` → `pytest e2e -q`
  (25 passed, 1 deselected) → `make e2e-down`, the benchmark run (all budgets met, exit 0). Proved
  clean: `lsof -nP -iTCP:8000 -sTCP:LISTEN` empty, `.autodev/e2e-server.pid` gone, `docker compose ps`
  empty. All output recorded in `QA-RESULTS.md`. `CHANGELOG.md`'s `0.1.0` section already had the
  benchmark numbers (T8) — nothing left to add.

## Verification

Commands, in the order T13 runs them:

```sh
uv run ruff check . && uv run ruff format --check .
uv run python -m django check --settings=tests.settings
uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings
uv run pytest -q
uv run pytest -q --cov=admin_errors --cov-fail-under=90
uv run tox -e package
make pg-up && DJANGO_DB=postgres ADMIN_ERRORS_TEST_PG_URL=postgres://postgres:postgres@localhost:5432/admin_errors_test uv run pytest -q ; make pg-down
make e2e-up && uv run --extra e2e pytest e2e -q ; make e2e-down
uv run python benchmarks/bench_capture.py
lsof -nP -iTCP:8000 -sTCP:LISTEN ; docker compose -f demo/docker-compose.yml ps
```

| Acceptance criterion | Proved by |
|---|---|
| `uv run pytest -q` green on SQLite | T13 gate run; 342 + new `tests/test_docs.py` cases |
| Green with `DJANGO_DB=postgres` against the compose service, container brought down | T12 run recorded in `QA-RESULTS.md`; `make pg-down` + `docker compose ps` output in T13 |
| `uv run tox -e package` exits 0 (build, twine check, clean-venv install, `check` + `migrate`) | T10; the env already exists, the task re-runs it |
| …proving templates, static files and the `uk` catalogue are in the wheel | T10's `tests/package_smoke.py`, run by the clean venv's interpreter as the env's last command |
| Exactly one migration; `makemigrations --check --dry-run` exits 0 | `test_exactly_one_migration_ships` (T1) + T9's recorded command output + the lint gate |
| Coverage ≥ 90 % | T13's `--cov-fail-under=90` run (baseline measured this session: 91 %) |
| No `# pragma: no cover` on capture or storage paths, asserted by a grep in the check | `test_no_pragma_no_cover_on_capture_or_storage_paths` (T1) — a grep, in the suite, so it also runs in CI |
| Django is the only runtime dependency, asserted by a test reading the metadata | `test_django_is_the_only_runtime_dependency` (T1) |
| Three `/boom/` hits → one issue, count 3 | `e2e/test_manual_qa.py::test_three_boom_hits_make_one_issue_with_count_three` (T11) |
| `/boom/1/` + `/boom/2/` → one issue | `…::test_boom_1_and_boom_2_group_into_one_issue` (T11) |
| `/storm/?n=5000` < 1 s and ≤ `EVENT_SAMPLE_PER_HOUR` stored events | `…::test_storm_is_fast_and_stores_few_events` (T11) |
| Resolve then re-hit → regressed badge | `…::test_resolve_then_rehit_shows_regressed_badge` (T11); the email clause is covered by `tests/test_notifications.py::test_regression_sends_one_email` (unobservable end-to-end under the demo's throttle, see p10-review_fix1 in DECISIONS.md) |
| `errors_cleanup --dry-run` prints a report | `…::test_errors_cleanup_dry_run_prints_a_report` (T11) |
| Dark mode readable | `…::test_dark_mode_is_readable` (T11) + the existing `e2e/test_screenshots.py` dark captures |
| The whole §13 script passes on **both** backends, results recorded | T11 (SQLite) and T12 (PostgreSQL), both written into `QA-RESULTS.md` with commands and output |
| README renders the `docs/img/` screenshots | `test_readme_embeds_every_committed_screenshot` (T1) |
| README settings table covers every key in `conf.py`, asserted against the defaults dict | `test_readme_settings_table_covers_every_setting` + `test_readme_settings_table_defaults_match_conf` (T1) |
| README contains the FAQ items | `test_readme_faq_maps_each_silent_failure_to_an_instrument` (T1) |
| `CHANGELOG.md` has a `0.1.0` section (with benchmark numbers) | `test_changelog_has_a_released_0_1_0_section`, `test_changelog_0_1_0_records_benchmark_numbers`, `test_version_matches_changelog` (T1/T8) |
| `make e2e-down` run, nothing listening on 8000, no containers left | T13's `lsof` and `docker compose ps` output pasted into `QA-RESULTS.md` |

## Risks

| Row | What this phase does |
|---|---|
| **#1 — wrong product, operator cannot triage** | Final closure. The §13 script becomes `e2e/test_manual_qa.py`, run against a live demo on both backends, plus the README FAQ that maps every silent failure to `errors_test` / `errors_stats` / a check ID. Prose alone would not close it; a re-runnable spec does. |
| **#12 — capture silently disabled in the host** | Closed here: FAQ item #1 covers `propagate: False` (→ `W002`), `DEBUG`/`CAPTURE_IN_DEBUG`, `CAPTURE_LEVEL`, `ENABLED`, each with the command that diagnoses it; `test_readme_faq_maps_each_silent_failure_to_an_instrument` keeps the mapping in the README. |
| **#17 — packaging defect ships** | `tox -e package` already builds/installs; T10 adds the assertion that templates, static files and the `uk` `.mo` actually resolve inside the clean venv, which is the part that was previously only implied by `migrate` succeeding. |
| **#19 — migration churn** | Verified, not rewritten: exactly one migration exists and is now pinned by a test as well as by the lint check. |
| **#16 — coverage gamed** | `--cov-fail-under=90` is run explicitly in T13, and the no-`pragma` grep becomes a test rather than a habit. No test is skipped or loosened in this phase. |
| **#10 — Django 4.2 EOL** | README compatibility section keeps the existing best-effort wording (already present) and it stays in the rewritten README. |
| **#20 — run stalls on the environment** | T12 and T13 both end in an explicit teardown whose output is recorded; `make e2e-up` is already idempotent. Any environment claim is backed by a pasted command output (rule 11). |
| **#15 — scope creep** | This phase adds no feature. The signals stay the documented extension point, and *Non-goals* is written from spec §2 verbatim in spirit. |

## Out of scope

- **Publishing to PyPI.** The roadmap's own assumption: the run ends at `python -m build` +
  `twine check` + clean-venv install. No `twine upload`, no trusted-publisher workflow, no tag.
- **Git tagging, releasing or committing.** The orchestrator commits (rule 6).
- **New locales.** Ukrainian remains the only shipped catalogue (roadmap assumption); `docs/user/`
  is English-only.
- **Product changes**, unless a doc or QA task exposes a genuine defect — then it is fixed in the
  product and noted in DECISIONS.md, but no feature, setting or model is added.
- **`docs/dev/{testing,development,operations}.md`** and further ADRs: the eight accepted ADRs plus
  `CLAUDE.md` and `.autodev/ARCHITECTURE.md` already cover the contributor path, and the guide says
  to create only pages with something true to say.
- **A second copy of `tests/test_admin.py` in e2e** (risk #14): the new e2e spec is the §13 script,
  nothing more.
