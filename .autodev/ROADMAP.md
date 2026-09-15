# Roadmap — django-admin-errors

Spec: `docs/spec.md` · generated 2026-09-15 12:32:53

`django-admin-errors` (import name `admin_errors`) is a zero-dependency reusable Django app that records unhandled exceptions and error-level log records into the host project's own database and shows them Sentry-style inside the Django admin — grouped by fingerprint, deduplicated, with traceback, request context, counts and 14/30-day trends. The roadmap is 10 dependency-ordered phases: a scaffold that makes `uv run pytest -q`, lint, build and the e2e harness work on a clean checkout; then the persistent contracts (settings proxy, models plus the single migration, frozen fingerprint algorithm); then the synchronous capture and storage pipeline; then the background writer with sampling, admission and signals; then retention, commands, the dedicated DB alias and Celery; a dedicated PostgreSQL pass while `storage.py` is still fresh; the demo project that becomes the browser surface; the admin UI with its permission gating and browser pass; notifications plus i18n; and finally docs, screenshots, the migration squash and release readiness. Spec §15's Phase 1 is split in two and its Phase 6 in two so every phase fits one focused session, and the demo project is pulled ahead of the admin UI because the intake makes it the e2e surface. Every spec acceptance criterion is owned by exactly one phase, SQLite is the per-phase gate, and PostgreSQL is verified in Phase 6 and again in Phase 10.

**Test command:** `uv run pytest -q`

## Assumptions

- Spec §15 Phase 1 is too large for one session (13 modules + 4 test modules), so it is split into Phase 2 (settings, models, migration, fingerprint) and Phase 3 (context, capture, storage, sync transport). Spec Phase 6 is likewise split into Phase 7 (demo project) and Phase 10 (docs and release).
- The demo project (Phase 7) is built before the admin UI (Phase 8), inverting spec §15: the intake makes the demo the e2e/browser surface and `make e2e-up` runs `demo_seed` before starting the server, so the demo must exist before the first real browser pass. The demo's URLs only exercise capture, which is complete after Phase 5.
- A dedicated `postgres-pass` phase (Phase 6) sits after retention, per the intake clarification and risk #5, so a PostgreSQL defect is found while `storage.py` is still fresh and a PG failure never blocks a SQLite phase.
- `demo/docker-compose.yml` (postgres:16) is created in Phase 6, ahead of the rest of `demo/`, because the PG pass needs a reproducible server and PROFILE.md fixes that file as its only location. `make e2e-up` already exits 0 when `demo/manage.py` is absent, so a lone compose file breaks nothing.
- Coverage `--cov-fail-under=90` is enforced in the CI workflow from Phase 1 but is not part of the per-phase `uv run pytest -q` gate, so a coverage dip fails the branch without masquerading as a functional failure.
- Phase 1's trivial passing e2e case is a browser-free spec in `e2e/` (harness import plus a reachability fixture that skips when nothing is listening), so `uv run --extra e2e pytest e2e -q` is green from Phase 1 without an `xfail` or an all-skipped suite.
- `__version__` starts at `0.1.0.dev0` and is bumped to `0.1.0` in Phase 10 together with the CHANGELOG promotion and the migration squash.
- Publishing to PyPI is out of scope (intake): Phase 10 ends at `python -m build` + `twine check` + a clean-venv wheel install. The PyPI name `django-admin-errors` is free, so the §3 fallback name is not used.
- Ukrainian is the only shipped locale catalogue; i18n markup and the catalogue land together in Phase 9 so templates are touched once.
- Risk closure: #10 #16 #17 #19 open in Phase 1 (matrix, CI coverage gate, package job, `makemigrations --check` in lint); #7 closed in Phase 2 (golden fingerprint tests); #2 and #3 closed for the capture half in Phase 3 (delegated scrubbing, never-raise/recursion tests); #6 and #11 closed in Phase 4 (injectable sink, `transaction=True`, pid-change test); #4 and #18 closed in Phase 5 (chunked deletes, retry-then-drop, four retention mechanisms); #5 closed in Phase 6; #14 and #20 closed in Phase 7 (idempotent `e2e-up`, `e2e-down` always runs); #1 #8 #9 and the render half of #2 closed in Phase 8 (browser pass, matrix rendering, `assertNumQueries` bounds, per-permission absence assertions); #13 closed in Phase 9; #12 and the final proof of #1 closed in Phase 10 (README FAQ, manual QA script, wheel install). #15 is held by every phase through the scope lists below.

## Phases

### 1. Scaffold and toolchain

**Goal:** A clean checkout builds, lints, tests and runs the e2e harness with every command from PROFILE.md working from the repository root, with no venv activated.

**User-facing:** no

**Deliverables:**
- `pyproject.toml`: hatchling, `src/` layout, `dependencies = ["Django>=4.2"]` only, extras `dev` (pytest, pytest-django, coverage, ruff, build, twine), `e2e` (pytest-playwright), `celery`, `postgres` (`psycopg[binary]`); explicit package data for templates/static/locale
- `[tool.ruff]` (line-length 100) and `[tool.pytest.ini_options]` with `DJANGO_SETTINGS_MODULE = tests.settings`
- `src/admin_errors/__init__.py` (`__version__ = "0.1.0.dev0"`, no side effects) and `apps.py` with an empty `AdminErrorsConfig` (verbose name "Errors")
- `tests/settings.py` — minimal host (`django.contrib.{admin,auth,contenttypes,sessions,messages}` only), SQLite default, PostgreSQL selected by `DJANGO_DB=postgres` + `ADMIN_ERRORS_TEST_PG_URL`; `tests/conftest.py`; one trivial passing unit test
- `e2e/conftest.py` + one browser-free passing e2e case (server-reachability fixture that skips when nothing listens)
- `tox.ini` with the full matrix from PROFILE.md plus `lint`, `postgres`, `celery`, `package` envs
- `.github/workflows/ci.yml`: lint, SQLite matrix, postgres service job, celery job, package job; coverage `--cov-fail-under=90`
- `Makefile` implementing the PROFILE.md contract: `e2e-up` (no-op exit 0 when `demo/manage.py` is absent), `e2e-down`, `test`, `test-pg`, `lint`, `build`, `demo`, `demo-pg` placeholders
- `.pre-commit-config.yaml` (ruff), `README.md` stub, `LICENSE` (MIT), `CHANGELOG.md` with an *Unreleased* section, `.gitignore`

**Acceptance criteria:**
- `uv run pytest -q` exits 0 from the repository root on a clean checkout with no venv activated and reports at least one passing test
- The full lint command (`ruff check` && `ruff format --check` && `django check --settings=tests.settings` && `makemigrations admin_errors --check --dry-run --settings=tests.settings`) exits 0
- `make e2e-up` exits 0 printing that `demo/` is not present yet; `uv run --extra e2e pytest e2e -q` exits 0; `make e2e-down` exits 0 and leaves no process listening on port 8000
- `uv run python -m build && uv run twine check dist/*` exits 0 and produces both a wheel and an sdist
- `uv run tox -e lint` exits 0
- `uv run python -c "import admin_errors; print(admin_errors.__version__)"` prints `0.1.0.dev0` without Django settings configured
- `.github/workflows/ci.yml` defines the lint, sqlite-matrix, postgres, celery and package jobs, and the sqlite matrix lists exactly the Python×Django cells from PROFILE.md

### 2. Settings proxy, models and fingerprint

**Goal:** Pin the two contracts that are expensive to change later: the database schema (one migration) and the fingerprint algorithm (frozen hash).

**User-facing:** no

**Deliverables:**
- `conf.py` — settings proxy over `settings.ADMIN_ERRORS` with every default from spec §5, re-reading on Django's `setting_changed` signal
- `checks.py` — `W001` (unknown setting key) and `E002` (SQLite < 3.9), registered from `AppConfig.ready()`
- `models.py` — `Issue`, `Event`, `IssueDailyCount` exactly per spec §6, including `Meta.ordering`, indexes, the `UniqueConstraint`, and the `view_issue_context` permission
- Exactly one initial migration in `src/admin_errors/migrations/`
- `fingerprint.py` — sha1 over the parts in spec §8, message normalization (UUIDs, hex, addresses, ISO-8601, digit runs, quoted literals, whitespace), message-only records keyed on `record.msg`, explicit override
- `tests/test_fingerprint.py` with golden hex values pinned in the file; `tests/test_settings_and_checks.py` for defaults, `override_settings` reload, W001 and E002
- CHANGELOG *Unreleased* entry

**Acceptance criteria:**
- `uv run pytest -q` green; `tests/test_fingerprint.py` covers every case in spec §14.2 including pinned golden hashes, both normalization families, same-message-two-culprits, two-types-one-message, message-template keying and the `fingerprint=` override
- `uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings` exits 0 and `src/admin_errors/migrations/` contains exactly one migration besides `__init__.py`
- A test asserts that after `migrate` the `Permission` table contains `admin_errors.view_issue_context` alongside the four default model permissions
- A test asserts `conf.settings` reflects a value changed with `override_settings(ADMIN_ERRORS={...})` and reverts afterwards
- `django check --settings=tests.settings` emits no warnings on the default test settings; targeted tests trigger W001 (unknown key) and E002 (patched SQLite version)

### 3. Capture pipeline and storage (sync transport)

**Goal:** An error raised anywhere in a host project becomes an Issue row, with scrubbed context, without the capture path ever raising or recursing.

**User-facing:** no

**Deliverables:**
- `context.py` — payload build per spec §6.4 via `ExceptionReporter.get_traceback_frames()`, scrubbing delegated to `get_exception_reporter_filter(request)`, safe `repr()` with `<unrepresentable …>` fallback, `in_app` detection, request/user/`extra` extraction, `DisallowedHost` fallback to path
- `capture.py` — guards, once-per-exception marker, thread-local recursion guard, ignore rules (loggers, exceptions, `status_code`), `BEFORE_SEND`, size enforcement and the documented degradation order (`vars` → context lines → message), `MAX_FRAMES` / `MAX_VAR_REPR_LENGTH`
- `api.py` — `capture_exception`, `capture_message`, `flush` (no-op under sync transport)
- `handlers.py` (`AdminErrorsHandler`), `middleware.py` (`RequestContextMiddleware`, sync+async capable), `got_request_exception` marker receiver
- `storage.py` — `store_batch(batch, *, using)` per spec §9.2: per-aggregate `atomic()`, savepointed racing create, counter/`last_seen` update, status transitions, event ring buffer trim, `last_event`, daily counts
- `AppConfig.ready()` installing the handler and connecting the marker receiver (no DB, no thread, no filesystem)
- `tests/test_context.py`, `tests/test_capture.py` (minus sampling/admission), `tests/test_storage.py` (minus the concurrency case)
- CHANGELOG *Unreleased* entry

**Acceptance criteria:**
- `uv run pytest -q` green; an unhandled view exception raised through `Client(raise_request_exception=False)` produces exactly one Issue with `source="exception"`, `logger="django.request"` and a request block
- Scrubbing tests assert the literal secret values are **absent** from the stored payload for: POST `password`, `@sensitive_post_parameters`, `@sensitive_variables` locals, `Authorization` / `Cookie` / `X-Api-Key` headers and the `sessionid` cookie
- Capture never raises and never recurses: with `storage.store_batch` monkeypatched to raise, the request still returns, no recursion occurs, and nothing propagates; a `__repr__` that raises yields the fallback string; a `request.user` that raises does not break capture
- `Http404`, `PermissionDenied`, `IGNORE_LOGGERS` (exact and prefix) and `status_code < 500` records are dropped; a log-and-re-raise produces exactly one occurrence; `ENABLED=False` and `CAPTURE_IN_DEBUG=False` are no-ops
- `test_storage.py` asserts the event ring buffer keeps exactly `EVENTS_PER_ISSUE` newest rows, `first_seen` is unchanged on update, a batch spanning UTC midnight creates two `IssueDailyCount` rows, a resolved issue reopens keeping `resolved_at`, an ignored issue gets counters only, and `assertNumQueries` upper bounds hold for the new-issue, count-only and three-sample paths
- An async view raising under `AsyncClient` is captured with request context
- The full lint command exits 0

### 4. Background writer, sampling and signals

**Goal:** Move all database I/O off the request path into a lazily started daemon thread, and bound admission and sampling so a storm costs one issue and a handful of events.

**User-facing:** no

**Deliverables:**
- `writer.py` — singleton `Writer` with `queue.Queue(QUEUE_MAXSIZE)`, injectable `sink`, lazy start on first `enqueue()`, pid comparison on every enqueue, aggregation by fingerprint, flush on interval / batch size / explicit request, `close_old_connections()` and idle connection close, one retry after 100 ms on `OperationalError` then drop, permanent recursion guard on the thread, `stats` counters
- Process-local bounded-LRU token buckets: `NEW_ISSUES_PER_MINUTE` admission and `EVENT_SAMPLE_PER_HOUR` sampling; count-only `CapturedItem` path
- `signals.py` with `issue_created`, `issue_regressed`, `issue_status_changed`; `on_commit` firing from `storage.py` with per-receiver exception swallowing
- `api.flush(timeout)` backed by a per-request `threading.Event`, registered once with `atexit`; `TRANSPORT="thread"` becomes the default
- `benchmarks/bench_capture.py` measuring capture latency with/without locals and `store_batch` throughput for 10k aggregated occurrences
- `tests/test_writer.py`, the `test_storage.py` concurrency case, sampling and admission cases in `test_capture.py`
- CHANGELOG *Unreleased* entry with the measured benchmark numbers

**Acceptance criteria:**
- `uv run pytest -q` green with `TRANSPORT="thread"` as the shipped default and `sync` as the test default
- `test_writer.py` covers lazy start, aggregation within the interval, flush on batch size, flush on explicit `flush()`, queue overflow dropping and counting, a raising sink being swallowed, a pid change restarting the thread, and `atexit` registered exactly once
- With `transaction=True`: an exception raised inside a deliberately broken `atomic()` block in the test thread is still stored by the writer thread, proving the separate connection
- With `transaction=True`: two threads storing the same new fingerprint concurrently produce exactly one Issue with `count=2`
- With `EVENT_SAMPLE_PER_HOUR=2`, 10 captures of one error yield `count=10` and exactly 2 Events; with `NEW_ISSUES_PER_MINUTE=3`, 10 unique errors yield 3 Issues and `stats.dropped_new_issue == 7`
- `issue_created` and `issue_regressed` fire only after commit (asserted with `captureOnCommitCallbacks`), and a receiver that raises does not break storage
- `uv run python benchmarks/bench_capture.py` prints a table and records capture p50 ≤ 2 ms without locals; the numbers are written into CHANGELOG

### 5. Retention, commands, dedicated alias and Celery

**Goal:** Storage stays bounded without operator attention, and the operator has instruments to inspect, clean and self-diagnose.

**User-facing:** no

**Deliverables:**
- `retention.py` — `run_cleanup(*, using, vacuum=False, dry_run=False) -> CleanupReport` with the six rules of spec §9.3 in order, deletes chunked at 1000 ids, eviction order `ignored → resolved → open` with hysteresis to `0.9 × MAX_ISSUES`, SQLite incremental/full vacuum handling
- Opportunistic cleanup in the writer thread: process-local timestamp plus `cache.add("admin_errors:cleanup-lock", …)`; honours `CLEANUP="off"`
- Management commands `errors_cleanup` (`--vacuum`, `--dry-run`, `--database`), `errors_stats` (row counts, oldest/newest, approximate size, writer stats), `errors_test` (one error through the full pipeline, prints the admin URL)
- `tasks.py` — `cleanup` `shared_task` defined only when Celery is importable
- `routers.py` (`AdminErrorsRouter`) plus check `E001` for a missing alias; check `W002` for the `propagate: False` trap
- `integrations/celery.py` — `task_failure` receiver adding the `celery` context, connected only when Celery imports
- `tests/test_retention.py`, `tests/test_commands.py`, `tests/test_routers.py`, `tests/test_celery.py`
- CHANGELOG *Unreleased* entry

**Acceptance criteria:**
- `uv run pytest -q` green; each retention rule is tested in isolation with a frozen clock, and eviction order plus hysteresis are asserted
- A chunked delete of 2500 rows stays within the `assertNumQueries` upper bound documented in the test
- `errors_cleanup --dry-run` prints per-step counts and deletes nothing; `errors_cleanup --vacuum` succeeds on SQLite with `PRAGMA auto_vacuum` both 0 and 2 (skipped on PostgreSQL)
- Opportunistic cleanup runs at most once per `CLEANUP_INTERVAL_SECONDS` per process and not at all with `CLEANUP="off"`
- `errors_test` creates an issue and prints its admin URL; `errors_stats` prints row counts and writer stats
- With `ADMIN_ERRORS["DATABASE"]` set to a second alias and the router installed, `admin_errors` rows land only in that alias and `migrate` on `default` creates no `admin_errors` tables; a missing alias raises `E001`
- `uv run tox -e celery` is green: an eager task failure produces one Issue with a `celery` context block and exactly one occurrence
- A `LOGGING` dict with `propagate: False` on `django.request` and no `admin_errors` handler triggers `W002`

### 6. PostgreSQL pass

**Goal:** Prove the storage and retention layers on PostgreSQL while they are still fresh, so no savepoint or aborted-transaction defect surfaces at the end of the run.

**User-facing:** no

**Deliverables:**
- `demo/docker-compose.yml` with a `postgres:16` service exposing `postgres://postgres:postgres@localhost:5432/admin_errors_test`
- `Makefile` `test-pg` target bringing the service up, running the suite with `DJANGO_DB=postgres`, and leaving no container running when invoked with the documented teardown
- Any fixes required in `storage.py` / `retention.py` / `models.py` to satisfy PostgreSQL semantics (savepointed racing creates, no `select_for_update`, `GREATEST`/`F()` usage, JSONField behaviour)
- PostgreSQL-specific assertions where SQLite cannot exercise the path (savepoint isolation after `IntegrityError`)
- `.github/workflows/ci.yml` postgres job verified against the same URL contract
- CHANGELOG *Unreleased* entry recording the pass and any fixes

**Acceptance criteria:**
- `docker compose -f demo/docker-compose.yml up -d` then `DJANGO_DB=postgres ADMIN_ERRORS_TEST_PG_URL=postgres://postgres:postgres@localhost:5432/admin_errors_test uv run pytest -q` exits 0 with the full suite green, and the container is brought down before the phase ends
- `uv run pytest -q` (SQLite) stays green — no regression from the PostgreSQL fixes
- The two-thread same-fingerprint race test passes on PostgreSQL, proving the savepoint handling survives `IntegrityError`
- `grep -rn "select_for_update" src/` returns no matches
- The `assertNumQueries` upper bounds in `test_storage.py` hold on PostgreSQL as well as SQLite
- `make e2e-up` still exits 0 with the no-op message (a lone `demo/docker-compose.yml` does not make `demo/manage.py` exist)

### 7. Demo project

**Goal:** A runnable demo project that exercises every capture behaviour by hand and becomes the browser surface for the remaining phases.

**User-facing:** yes — gets end-to-end cases and user docs

**Deliverables:**
- `demo/manage.py`, `demo_project/{settings,urls,celery}.py` — SQLite by default, `DEMO_DB=postgres` switching to the compose service, console email backend, `ADMINS` set, `RequestContextMiddleware` enabled, small retention values per spec §13
- `demo_app/views.py` with every URL from spec §13 (`/`, `/boom/`, `/boom/<int:n>/`, `/keyerror/<slug>/`, `/nested/`, `/logged/`, `/warning/`, `/sensitive/`, `/storm/`, `/unique-storm/`, `/task/`, `/async-boom/`, `/404/`) and an index page listing them
- `demo_app/tasks.py` with `fail_task` (eager by default)
- `demo_app/management/commands/demo_seed.py --issues --days [--reset]` using the real pipeline with `TRANSPORT="sync"`, then adjusting timestamps, creating random-walk daily counts, marking some issues resolved/ignored/regressed, and creating superuser `admin`/`admin`
- `Makefile` `demo`, `demo-pg` and the full `e2e-up` path (migrate → seed → playwright install → background runserver → poll the ready URL) now exercised for real
- `tests/test_demo.py` driving every demo URL against the demo settings on SQLite
- `e2e/` gains a real login case against the running demo
- User documentation: a README "Try it" section describing `make demo` and the demo URL map
- CHANGELOG *Unreleased* entry

**Acceptance criteria:**
- `uv run pytest -q` green, including `tests/test_demo.py`, which asserts every demo URL responds with the expected status (500 where expected, 200 for `/logged/`, 404 for `/404/`) and the expected issue outcome: `/boom/1/` and `/boom/2/` collapse to one issue, `/keyerror/<slug>/` collapses to one, `/404/` produces none
- `demo_seed --issues 40 --days 30` creates 40 issues with daily counts spread over 30 days and a superuser `admin`/`admin`; re-running with `--reset` is idempotent
- `make e2e-up` migrates, seeds, starts the server, and the ready URL `http://127.0.0.1:8000/admin/login/` answers within 45 s; it exits 0 a second time without starting a duplicate server
- `uv run --extra e2e pytest e2e -q` passes a real browser case that logs in as `admin`/`admin` and reaches the admin index
- `make e2e-down` kills the server, removes `.autodev/e2e-server.pid`, and nothing is left listening on port 8000
- `/storm/?n=5000` returns in under 1 second and grows the database by at most 1 issue, `EVENT_SAMPLE_PER_HOUR` events and 1 daily count row

### 8. Admin UI

**Goal:** The operator can triage from the Django admin: find the issue, read the traceback and context, and resolve, ignore or reopen it — with sensitive context gated behind its own permission.

**User-facing:** yes — gets end-to-end cases and user docs

**Deliverables:**
- `admin.py` — `IssueAdmin` (list columns, `list_filter` incl. `LastSeenFilter`, `search_fields`, ordering, `list_per_page`, `has_add_permission = False`, fully read-only form), `register(site)`, `ADMIN_SITE` resolution, resolve/ignore/reopen actions and the `…/<pk>/status/<action>/` and `…/<pk>/events/<event_id>/` views wrapped in `site.admin_view`
- `templatetags/admin_errors_tags.py` — sparkline, level/status badges, humanized values
- Templates: `change_list.html` (summary cards), `change_form.html` (issue detail), `event_detail.html` and the `includes/` partials for traceback, request, events, chart and cards
- `static/admin_errors/{admin_errors.css,admin_errors.js}` — admin CSS variables only, no CDN, no inline script
- Permission gating for `view_issue`, `view_issue_context`, `change_issue`, `delete_issue`, enforced in both views and templates
- `tests/test_admin.py` per spec §14.2
- e2e cases from PROFILE.md: issue list with cards and sparkline, three `/boom/` hits → count 3, `/boom/1/` + `/boom/2/` collapsing, detail traceback with collapsed library frame and locals toggle, chained-exception separator on `/nested/`, resolve then re-hit → regressed badge, a `view_issue`-only user seeing no context section and getting 403 on the status POST
- Screenshots of the issue list and issue detail in light and dark into `docs/img/`
- CHANGELOG *Unreleased* entry

**Acceptance criteria:**
- `uv run pytest -q` green, including `tests/test_admin.py`: list renders cards, badges and sparklines; every filter and search field works; detail renders traceback, chained exceptions, request section, events list and chart; event detail renders
- `assertNumQueries` upper bounds hold: ≤ 12 for a list page of 50 issues and ≤ 15 for the detail page — no N+1 from the 14-day sparkline
- Permission tests assert by absence: with `view_issue` only, the response body contains none of the header, cookie, POST, locals or user values; adding `view_issue_context` makes them present; without `change_issue` the status buttons are absent and a POST returns 403; without `delete_issue` the same for delete
- The resolve / ignore / reopen actions (bulk and single) update the rows and fire `issue_status_changed`
- `register(custom_site)` works and `ADMIN_SITE=False` skips the default registration
- `make e2e-up && uv run --extra e2e pytest e2e -q && make e2e-down` is green, covering the browser cases listed in the deliverables, and leaves nothing listening on port 8000
- `docs/img/` contains issue-list and issue-detail screenshots in light and dark taken from the seeded demo, and the phase notes record that both themes were checked visually
- `static/admin_errors/admin_errors.css` is ≤ 6 KB and `admin_errors.js` is ≤ 3 KB, asserted by a test

### 9. Notifications, status transitions and i18n

**Goal:** The operator hears about a new issue or a regression once, not on every occurrence, and the UI is fully translatable with a Ukrainian catalogue.

**User-facing:** yes — gets end-to-end cases and user docs

**Deliverables:**
- `notifications.py` — `EmailNotifier` connected to `issue_created` / `issue_regressed` per `NOTIFY_ON`, throttled by a conditional `UPDATE … WHERE notified_at IS NULL OR notified_at < threshold` performed before sending, plain-text body with title, culprit, count, first/last seen, short traceback and the admin URL
- `NOTIFY_BACKEND`, `NOTIFY_RECIPIENTS`, `NOTIFY_THROTTLE_SECONDS`, `NOTIFY_ON`, `NOTIFY_BASE_URL` settings and their defaults in `conf.py`
- Check `W003` — `mail_admins` handler still active while `EmailNotifier` is enabled
- `issue_status_changed` fired from every admin mutation path; the "Copy as text" server-rendered plain traceback, gated the same way as the traceback itself
- `{% load i18n %}` / `{% translate %}` in every template, `gettext_lazy` for Python-side UI strings, and `locale/uk/LC_MESSAGES/django.po` + `.mo` with the UI translated
- `tests/test_notifications.py` per spec §14.2
- CHANGELOG *Unreleased* entry

**Acceptance criteria:**
- `uv run pytest -q` green; a new issue sends exactly one email to `ADMINS` with the expected subject and body fields, a regression sends one, and an ignored issue sends none
- A second occurrence within `NOTIFY_THROTTLE_SECONDS` sends no email; one after the window sends one
- With `transaction=True`, two threads notifying concurrently for the same issue send exactly one email
- `NOTIFY_BACKEND=None` disables notifications and a custom dotted path is loaded and used
- `uv run python -m django makemessages -l uk` produces no new untranslated UI strings — every `msgid` in `locale/uk/LC_MESSAGES/django.po` has a non-empty `msgstr`, asserted by a test
- Admin pages render without error under `LANGUAGE_CODE="uk"`, asserted in `test_admin.py`
- The "Copy as text" traceback is present for a user with `view_issue_context` and contains no locals for a user with `view_issue` only
- A `LOGGING` dict with a `mail_admins` handler while `EmailNotifier` is enabled triggers `W003`

### 10. Documentation, release readiness and final verification

**Goal:** Ship-ready 0.1.0: one migration, complete README with screenshots, a wheel that installs and migrates in a clean environment, and the manual QA script passing on both backends.

**User-facing:** yes — gets end-to-end cases and user docs

**Deliverables:**
- `README.md` per spec §17: what it is and when to use it, screenshots from `docs/img/`, install steps, the ASCII pipeline diagram, the full settings table, permissions and groups, retention / `errors_cleanup` / Celery beat / dedicated alias / SQLite tips (WAL, `timeout`, `auto_vacuum`), notifications and signals incl. replacing `mail_admins`, Celery / ASGI / gunicorn notes, the storage bound and benchmark numbers, the FAQ mapping each silent failure to its instrument, non-goals and contributing
- User documentation in `docs/user/` for the operator-facing workflows (triage, retention, notifications)
- Migration squash to exactly one initial migration; `__version__` bumped to `0.1.0`; `CHANGELOG.md` `0.1.0` section promoted from *Unreleased* with the benchmark numbers
- A final PostgreSQL pass and the spec §13 manual QA script executed end-to-end on both backends, with the results recorded
- Clean-venv wheel install verification wired as the `package` tox env / CI job

**Acceptance criteria:**
- `uv run pytest -q` green on SQLite and green with `DJANGO_DB=postgres` against the compose service; the container is brought down before the phase ends
- `uv run tox -e package` exits 0: `python -m build`, `twine check dist/*`, install the wheel into a clean venv, then `django-admin check` and `migrate` on a minimal project succeed — proving templates, static files and the `uk` catalogue are in the wheel
- `src/admin_errors/migrations/` contains exactly one migration, and `makemigrations admin_errors --check --dry-run` exits 0 against it
- Coverage for `src/admin_errors` is ≥ 90 % (`uv run pytest -q --cov=admin_errors --cov-fail-under=90`) with no `# pragma: no cover` on capture or storage paths, asserted by a grep in the check
- `pyproject.toml` lists Django as the only runtime dependency (asserted by a test reading the metadata)
- The spec §13 manual QA script passes end-to-end: three `/boom/` hits → one issue with count 3; `/boom/1/` + `/boom/2/` → one issue; `/storm/?n=5000` → response < 1 s and ≤ `EVENT_SAMPLE_PER_HOUR` stored events; resolve then re-hit → regressed badge plus a console email; `errors_cleanup --dry-run` prints a report; dark mode readable
- `README.md` renders the `docs/img/` screenshots, contains the settings table covering every key in `conf.py` (asserted by a test comparing the table against the defaults dict) and the FAQ items, and `CHANGELOG.md` has a `0.1.0` section
- `make e2e-down` has been run and nothing is listening on port 8000; no containers left running
