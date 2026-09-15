# Phase 7 — Demo project

**Goal:** a runnable demo Django project that exercises every capture behaviour by hand, and becomes
the browser surface for Phases 8–10.

## Context

**What exists.** The library is feature-complete for capture (Phases 2–6): `conf.py` (settings
proxy), `models.py` + one migration, `fingerprint.py`, `context.py`, `capture.py`, `api.py`
(`capture_exception` / `capture_message` / `flush`), `handlers.py`, `middleware.py`
(`RequestContextMiddleware`), `writer.py` (thread transport, admission + sampling buckets),
`storage.py`, `retention.py`, `routers.py`, `signals.py`, `tasks.py`, `integrations/celery.py` and
the three management commands. 208 tests pass on SQLite, 216 on PostgreSQL. There is **no admin UI
yet** (Phase 8) — `admin_errors` is not registered on any admin site, so the demo's admin shows only
auth models until Phase 8 lands. That is expected and does not block this phase.

**What exists around the demo.** `demo/` currently holds only `docker-compose.yml` (`postgres:16`,
Phase 6). `Makefile` already has the full `e2e-up` recipe (guarded by `test -f demo/manage.py`, so it
prints a skip message today), `e2e-down`, `pg-up`, `pg-down`, `test-pg`, and `demo` / `demo-pg`
placeholders that echo "demo/ arrives in Phase 7". `e2e/conftest.py` has a browser-free
`server_available` fixture; `e2e/test_harness.py` has two urllib-only cases. `tests/settings.py` is
the minimal host (`ROOT_URLCONF = tests.urls`, `ADMIN_ERRORS = {"TRANSPORT": "sync"}`).

**What this phase changes.**
- Creates `demo/` as a real Django project (`manage.py`, `demo_project/`, `demo_app/`).
- Turns the `demo` / `demo-pg` Makefile placeholders into real targets and makes the existing
  `e2e-up` path run for real for the first time.
- Adds `tests/test_demo.py` to the main gate, driving every demo URL and `demo_seed`.
- Adds a real browser login case to `e2e/`.
- README "Try it" section + CHANGELOG entry.

**Key files (new unless marked).**

```
demo/manage.py
demo/demo_project/{__init__,settings,urls,celery}.py
demo/demo_app/{__init__,apps,views,tasks,urls}.py
demo/demo_app/templates/demo_app/index.html
demo/demo_app/management/{__init__.py,commands/{__init__.py,demo_seed.py}}
tests/test_demo.py
e2e/test_admin_login.py
Makefile                 (modified: demo, demo-pg, e2e-up seeds with --reset)
pyproject.toml           (modified: pytest pythonpath, sdist exclude)
tests/test_toolchain.py  (modified: PG URL consistency now covers the demo settings)
README.md CHANGELOG.md   (modified)
```

## Design

### Module layout

`demo/` is a conventional Django project rooted at `demo/`, so `demo/manage.py` puts `demo/` on
`sys.path` and `demo_project` / `demo_app` are top-level importable from there. Nothing in `demo/`
is shipped: `pyproject.toml`'s sdist already excludes `demo/`, and the wheel only packages
`src/admin_errors`.

### `demo_project/settings.py`

Plain module-level constants (no `django.setup()`, no side effects), so `tests/test_demo.py` can
`import demo_project.settings` and reuse the *real* values instead of re-declaring them:

- `DATABASES`: SQLite at `demo/demo.sqlite3` by default (gitignored via `*.sqlite3`).
  `DEMO_DB=postgres` switches to `DEMO_PG_URL`, default
  `postgres://postgres:postgres@localhost:5432/admin_errors_test` — the same compose service and
  database Phase 6 created. A private `_database_from_url()` parser is duplicated from
  `tests/settings.py` rather than imported: the demo must not depend on the test package.
- `DEBUG = True` by default (`DEMO_DEBUG=0` flips it), `ALLOWED_HOSTS = ["*"]`. `runserver` only
  serves the admin's static files with `DEBUG=True`, and the technical 500 page is part of what the
  demo shows off.
- `INSTALLED_APPS`: `django.contrib.{admin,auth,contenttypes,sessions,messages,staticfiles}`,
  `admin_errors`, `demo_app`.
- `MIDDLEWARE`: Django's default seven plus
  `admin_errors.middleware.RequestContextMiddleware` last.
- `ADMINS = [("Demo Admin", "admin@example.com")]`,
  `EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"` — Phase 9's notifier prints to
  the console.
- `ADMIN_ERRORS` per spec §13: `TRANSPORT="thread"`, `CAPTURE_LEVEL="WARNING"`,
  `EVENT_SAMPLE_PER_HOUR=5`, `EVENT_RETENTION_DAYS=7`, `MAX_ISSUES=200`, `INTERNAL_LOGGING=True`.
  Everything else stays on the shipped defaults, so the demo also demonstrates them.
- `LOGGING`: console handler at INFO for `demo_app` and `django.request`; no `propagate: False`
  anywhere (that is the `W002` trap the demo must not model).
- `SECRET_KEY` is a literal `"demo-only-not-a-secret"` with a comment — the demo is never deployed.

`demo_project/urls.py`: `admin/` + `include("demo_app.urls")`.
`demo_project/celery.py`: `app = Celery("demo")` with `task_always_eager = True`, defined only when
`celery` imports; `demo_project/__init__.py` imports it inside a `try/except ImportError` so the
project runs with zero extras installed.

### `demo_app/views.py` — the URL map (spec §13)

| URL | name | Behaviour | Expected status |
|---|---|---|---|
| `/` | `index` | template listing every link with one-line descriptions | 200 |
| `/boom/` | `boom` | `1 / 0` → `ZeroDivisionError` | 500 |
| `/boom/<int:n>/` | `boom_n` | `raise ValueError(f"bad value {n}")` | 500 |
| `/keyerror/<slug:key>/` | `keyerror` | `{}[key]` → `KeyError` | 500 |
| `/nested/` | `nested` | `RuntimeError` raised `from` an inner `ValueError` | 500 |
| `/logged/` | `logged` | catches, `logger.exception(...)`, returns 200 | 200 |
| `/warning/` | `warning` | `logger.warning("Slow payment provider %s", name, extra={...})` | 200 |
| `/sensitive/` | `sensitive` | GET renders a POST form (`password`, `token`); POST is `@sensitive_post_parameters("password")` + `@sensitive_variables("token")` and raises | 200 / 500 |
| `/storm/?n=` | `storm` | loop of `n` fresh same-type exceptions through `api.capture_exception`; returns elapsed seconds | 200 |
| `/unique-storm/?n=` | `unique_storm` | `n` `capture_message` calls with `fingerprint=f"demo-unique-storm-{i}"` | 200 |
| `/task/` | `task` | dispatches `demo_app.tasks.fail_task` | 200 |
| `/async-boom/` | `async_boom` | `async def` view raising `RuntimeError` | 500 |
| `/404/` | `not_found` | `raise Http404` — must not be captured | 404 |

Design points that are load-bearing:

- **Storm raises a fresh exception per iteration.** `capture.py` marks each exception object as
  captured once (`_mark_captured_once`), so a single exception object reused 5000 times would be
  captured once. The loop is `try: raise DemoStormError(...) except DemoStormError: capture_exception()`
  inside the loop body, which is also what a real retry loop looks like.
- `/storm/` returns a small `JsonResponse` with `n`, `elapsed_seconds` and the fingerprint, so both
  the browser and `tests/test_demo.py` can read the measured time.
- `n` is parsed with a default (1000 for storm, 200 for unique-storm) and clamped to 100 000 so a
  stray query string cannot hang the demo.
- `/sensitive/` puts the secret literals in fixed constants (`DEMO_PASSWORD`, `DEMO_TOKEN`) and sets
  an `Authorization` header expectation in its description, so the test can assert those literals
  are **absent** from the stored payload.

`demo_app/tasks.py`: `fail_task` is a `shared_task(name="demo_app.fail_task")` when `celery`
imports, otherwise a plain function with the same body (`raise DemoTaskError(...)`). The `/task/`
view calls `fail_task.delay()` when `delay` exists, else calls the function inside
`try/except` + `api.capture_exception()`. Both paths produce exactly one issue, so the demo and
`tests/test_demo.py` work with and without the `celery` extra.

### `demo_app/management/commands/demo_seed.py`

`demo_seed --issues N --days D [--reset]`:

1. `--reset` deletes `Event`, `IssueDailyCount`, `Issue` rows on `conf.DATABASE` first (never users).
2. Wraps the whole body in `override_settings(ADMIN_ERRORS={**settings.ADMIN_ERRORS, "TRANSPORT":
   "sync", "NEW_ISSUES_PER_MINUTE": None, "EVENT_SAMPLE_PER_HOUR": 50})` — the real pipeline, but
   synchronous (spec §13) and with the admission limiter off, so `--issues 100` is not silently
   capped at the default 50/minute. `django.test.utils.override_settings` in a management command is
   acceptable here because `demo/` is never shipped.
3. Creates the issues through the real pipeline: for each `i` it raises one of a fixed list of
   exception classes from one of a fixed list of culprit functions with a message built from a fixed
   word list (`f"{verb} {noun} failed"`), then `api.capture_exception()`. Distinct culprits and
   distinct non-normalizable words give distinct fingerprints deterministically — no explicit
   `fingerprint=` override, so the shipped grouping algorithm is what produces the demo data.
   Each issue gets 1–4 captures so some have events and some do not.
4. Rewrites the timestamps and counts with `random.Random(20260915)` (a fixed seed): `first_seen`
   spread over the last `D` days, `last_seen` between `first_seen` and now, `count` = the sum of the
   generated daily counts, `IssueDailyCount` rows from a random walk over the issue's active days,
   `Event.timestamp` pulled back into the issue's window.
5. Marks statuses: every 7th issue `resolved` (with `resolved_at`), every 11th `ignored`, every 13th
   "regressed" (status `open` with a `resolved_at` in the past and `last_seen` after it).
6. `create_superuser("admin", "admin@example.com", "admin")` when no `admin` user exists; otherwise
   resets that user's password to `admin` so the e2e login case is never at the mercy of an old
   database file.

Determinism is the point: with the fixed seed, the fixed word list and the real fingerprint
algorithm, `demo_seed --reset` twice in a row produces byte-identical issue/daily-count data. That
is what makes the idempotency criterion testable and Phase 8's screenshots reproducible.

### `tests/test_demo.py`

One pytest session runs the whole gate, so the demo cannot have its own `DJANGO_SETTINGS_MODULE`.
Instead the test module imports the demo settings as data and re-applies the parts that matter:

```python
import demo_project.settings as demo_settings

DEMO = override_settings(
    ROOT_URLCONF="demo_project.urls",
    MIDDLEWARE=demo_settings.MIDDLEWARE,
    INSTALLED_APPS=[*tests_settings.INSTALLED_APPS, "demo_app"],
    ADMIN_ERRORS={**demo_settings.ADMIN_ERRORS, "TRANSPORT": "sync"},
)
```

- `pyproject.toml` gains `pythonpath = [".", "demo"]` so `demo_project` / `demo_app` import under
  plain `uv run pytest -q`.
- Overriding `INSTALLED_APPS` is enough to register `demo_app`: Django's `update_installed_apps`
  receiver (`django/test/signals.py`) calls `apps.set_installed_apps`, clears
  `get_commands.cache_clear()` and `get_app_template_dirs.cache_clear()`, so both `call_command("demo_seed")`
  and `demo_app/templates/` resolve. `demo_app` has no models, so no migration is involved. If the
  command lookup ever proves fragile, the fallback is `call_command(DemoSeedCommand(), ...)` with an
  instance — no test is weakened either way.
- `TRANSPORT` is forced to `"sync"` for the URL sweep (project convention, CLAUDE.md) so every
  assertion runs on a settled database with no thread. The one case that must prove the thread
  transport (the 5000-storm timing criterion) opts in explicitly with
  `django_db(transaction=True)` + `TRANSPORT="thread"` + `api.flush()`, the pattern already used in
  `tests/test_writer.py` and `tests/test_storage.py`.
- `Client(raise_request_exception=False)` for the 500 cases; `AsyncClient` for `/async-boom/`
  (mirrors the existing `tests/test_capture.py` async case).

### Error handling

Nothing in `demo/` may weaken the library's guarantees: the demo never touches `admin_errors`
internals, only `admin_errors.api`. `demo_seed` lets its own exceptions propagate (a broken seed must
fail loudly), while the captures it performs go through the same never-raise pipeline as production.

### Architecture conformance

`.autodev/ARCHITECTURE.md` describes `demo/` as "a full Django project (spec §13) used for manual QA,
the README screenshots and the PostgreSQL target", which is exactly what this builds. No deviation
from the architecture. Three choices are *additions* the architecture does not cover — the demo
running with `DEBUG=True`, `demo-pg` reusing the compose `admin_errors_test` database, and
`tests/test_demo.py` reaching the demo through `override_settings` rather than a second settings
module — all appended to `.autodev/DECISIONS.md` under `## p07-plan`.

## Tasks

- [x] **T1: project skeleton.** `demo/manage.py`, `demo_project/__init__.py` (guarded celery import),
  `demo_project/settings.py` (everything in the Design section), `demo_project/urls.py`,
  `demo_project/celery.py`. Check: `uv run python demo/manage.py check` exits 0 and
  `DEMO_DEBUG=0 uv run python demo/manage.py check --deploy` does not error out.
- [x] **T2: `demo_app` package and the URL map.** `demo_app/{__init__,apps,urls}.py`,
  `demo_app/views.py` with all 13 views, `demo_app/templates/demo_app/index.html` listing every URL
  with its one-line description (plain Django template, no JS, admin-neutral CSS). Check:
  `uv run python demo/manage.py show_urls`-equivalent is not available, so check via
  `uv run python demo/manage.py check` plus T7's tests.
- [x] **T3: `demo_app/tasks.py` + `/task/` wiring.** `fail_task` conditional on Celery, view falling
  back to a direct call; `demo_project/celery.py` sets `task_always_eager`.
- [x] **T4: `demo_seed`.** `demo_app/management/commands/demo_seed.py` with `--issues`, `--days`,
  `--reset`, deterministic RNG, real-pipeline capture, timestamp/daily-count rewriting, status
  marking, superuser creation.
- [x] **T5: Makefile + packaging.** Replace the `demo` / `demo-pg` placeholders with real recipes
  (`migrate --noinput` → `demo_seed --issues 40 --days 30` → `runserver 127.0.0.1:8000`; `demo-pg`
  = `pg-up` then the same with `DEMO_DB=postgres`); make `e2e-up` seed with `--reset`. Add
  `pythonpath = [".", "demo"]` to `[tool.pytest.ini_options]` and `tests/test_demo.py` to the sdist
  `exclude` list (the sdist excludes `demo/`, so a `demo_project` import from `tests/` would break
  an sdist-only test run).
- [x] **T6: PG URL consistency.** Extend `tests/test_toolchain.py::test_postgres_url_is_consistent`
  (currently compose + CI + `tests/settings.py`) to also cover `demo_project/settings.py`'s default
  `DEMO_PG_URL`.
- [x] **T7: `tests/test_demo.py` — URL sweep.** The `DEMO` override helper plus the status/outcome
  cases listed in Verification (happy path, boundaries, errors, scrubbing, the `/404/` negative).
- [x] **T8: `tests/test_demo.py` — storm and limiters.** Small-`n` storm under sync transport
  (1 issue, `count == n`, events == `EVENT_SAMPLE_PER_HOUR`, 1 daily count row); `/unique-storm/`
  bounded by `NEW_ISSUES_PER_MINUTE`; the 5000-storm timing case under `TRANSPORT="thread"` +
  `transaction=True`.
- [x] **T9: `tests/test_demo.py` — `demo_seed`.** 40 issues / 30 days / superuser; `--reset`
  idempotency by snapshot comparison; `--issues 100` is not capped by the admission limiter.
- [x] **T10: e2e browser login.** `e2e/conftest.py` gained a `base_url` fixture returning
  `E2E_BASE_URL`; `e2e/test_admin_login.py` (`pytest.importorskip("playwright.sync_api")`, depends on
  `server_available`) fills the admin login form as `admin`/`admin`, submits, and asserts the admin
  index (`#site-name`) is reached and the URL is `/admin/`. Confirmed this session end to end:
  `make e2e-up` migrated, seeded 40 issues, started the server on :8000, ready URL answered;
  `uv run --extra e2e pytest e2e -q` → `3 passed` (`test_admin_login.py::test_admin_login_reaches_the_index`
  plus the two `test_harness.py` cases), verified with `-v` that the login case actually ran (not
  skipped); a second `make e2e-up` exited 0 with "server already answering", `lsof` showed exactly one
  pid; `make e2e-down` killed it, removed the pidfile, `lsof` empty afterwards.
- [x] **T11: docs.** README "Try it" section (`make demo`, the credentials, the URL map table, the
  spec §13 manual QA walk-through, `make demo-pg`), README dev table row for `make demo`, and the
  CHANGELOG *Unreleased* entry.
- [x] **T12: verification.** Ran the whole Verification section this session: `uv run pytest -q` →
  `238 passed, 8 skipped` (skips are the `postgres_only`-gated cases, expected on SQLite); `make lint`
  clean (ruff check, ruff format --check, `django check`, `makemigrations --check --dry-run`);
  `uv run python demo/manage.py check` → 0 issues, `DEMO_DEBUG=0 ... check --deploy` → 5 expected
  HTTPS/cookie warnings, exit 0; `make e2e-up` → migrated, seeded 40 issues, server up on :8000, ready
  URL answered; `uv run --extra e2e pytest e2e -q` → `3 passed` (verified with `-v` that
  `test_admin_login.py` actually ran, not skipped); second `make e2e-up` → exit 0, "server already
  answering", exactly one pid on `lsof`; `make e2e-down` → pid killed, pidfile removed, `lsof` empty;
  `make pg-up` → container healthy; `DEMO_DB=postgres manage.py migrate --noinput` → all migrations
  applied; `DEMO_DB=postgres manage.py demo_seed --issues 40 --days 30 --reset` → "seeded 40 issues";
  `make pg-down` → container removed, `docker ps` empty. Final check: `lsof -nP -iTCP:8000
  -sTCP:LISTEN` empty, `.autodev/e2e-server.pid` absent, no stray `runserver`/`manage.py` process.

## Verification

```sh
uv run pytest -q
uv run ruff check . && uv run ruff format --check . \
  && uv run python -m django check --settings=tests.settings \
  && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings
uv run python demo/manage.py check
make e2e-up && uv run --extra e2e pytest e2e -q
make e2e-up                     # second call: exits 0, no duplicate server
make e2e-down
lsof -nP -iTCP:8000 -sTCP:LISTEN ; test ! -f .autodev/e2e-server.pid
make pg-up && DEMO_DB=postgres uv run python demo/manage.py migrate --noinput \
  && DEMO_DB=postgres uv run python demo/manage.py demo_seed --issues 40 --days 30 --reset \
  && make pg-down
```

`make demo` and `make demo-pg` block on `runserver` by design and cannot be run to completion in an
unattended step; their substance (migrate → seed → background `runserver` → ready-URL poll) is
exactly what `make e2e-up` executes for real, and the recipes themselves are pinned by a toolchain
test. The PostgreSQL command above proves the `DEMO_DB=postgres` half of `demo-pg` without the
blocking server.

| Acceptance criterion | Test / command that proves it |
|---|---|
| `uv run pytest -q` green including `tests/test_demo.py` | the gate |
| every demo URL responds with the expected status | `test_demo.py::test_every_demo_url_returns_its_expected_status` (parametrized over the full URL map, incl. 200 for `/`, `/logged/`, `/warning/`, `/storm/`, `/unique-storm/`, `/task/`; 500 for `/boom/`, `/boom/7/`, `/keyerror/x/`, `/nested/`, `/sensitive/` POST, `/async-boom/`; 404 for `/404/`) |
| `/boom/1/` and `/boom/2/` collapse to one issue | `test_boom_n_collapses_to_one_issue` (asserts 1 `Issue`, `count == 2`) |
| `/keyerror/<slug>/` collapses to one issue | `test_keyerror_collapses_to_one_issue` (two different slugs → 1 issue, `count == 2`) |
| `/404/` produces no issue | `test_http404_produces_no_issue` |
| `/logged/` returns 200 and still captures | `test_logged_returns_200_and_captures_one_issue` |
| `/warning/` captured at level `warning` (`CAPTURE_LEVEL="WARNING"`) | `test_warning_is_captured_at_warning_level` |
| `/nested/` stores the chained exception | `test_nested_stores_exception_chain` (payload `chain` non-empty) |
| `/sensitive/` leaks nothing | `test_sensitive_view_payload_contains_no_secrets` (asserts `DEMO_PASSWORD`, `DEMO_TOKEN` and the `Authorization` value are absent from the serialized payload) |
| `/async-boom/` captured under `AsyncClient` | `test_async_boom_is_captured` |
| `/task/` produces exactly one issue with or without Celery | `test_task_view_produces_one_issue` |
| `/unique-storm/` bounded by `NEW_ISSUES_PER_MINUTE` | `test_unique_storm_is_bounded_by_new_issue_admission` |
| `/storm/?n=5000` returns in under 1 s | `test_storm_of_5000_returns_under_one_second` (wall-clock around the request **and** the view's own reported `elapsed_seconds`, `TRANSPORT="thread"`, `transaction=True`) |
| …and grows the DB by ≤ 1 issue, ≤ `EVENT_SAMPLE_PER_HOUR` events, 1 daily count row | same test, after `api.flush()`: deltas asserted against the pre-request counts |
| storm aggregation exactness on the sync path | `test_storm_aggregates_into_one_issue` (`n=25`: 1 issue, `count == 25`, events == 5, 1 daily count) |
| `demo_seed --issues 40 --days 30` creates 40 issues with daily counts over 30 days | `test_demo_seed_creates_issues_and_daily_counts` (40 issues, `IssueDailyCount` dates span ≥ 20 distinct days, none older than 30 days, none in the future) |
| …and a superuser `admin`/`admin` | `test_demo_seed_creates_superuser_admin` (`authenticate(username="admin", password="admin")` is a superuser) |
| re-running with `--reset` is idempotent | `test_demo_seed_reset_is_idempotent` (snapshot of `(fingerprint, count, status, sorted daily counts)` equal across two `--reset` runs) |
| seeding is not capped by the admission limiter | `test_demo_seed_is_not_capped_by_new_issue_admission` (`--issues 60` under the default 50/min → 60 issues) |
| `make e2e-up` migrates, seeds, starts the server, ready URL answers within 45 s | the `make e2e-up` command above (Verification block) |
| `make e2e-up` exits 0 a second time without a duplicate server | second `make e2e-up`; `lsof -nP -iTCP:8000 -sTCP:LISTEN` shows exactly one pid |
| the `demo` / `demo-pg` recipes really migrate, seed and serve | `tests/test_toolchain.py::test_makefile_demo_targets_run_the_real_project` (recipe contains `demo/manage.py migrate`, `demo_seed`, `runserver`; `demo-pg` depends on `pg-up` and sets `DEMO_DB=postgres`) + the PostgreSQL migrate/seed command above |
| a real browser logs in as `admin`/`admin` and reaches the admin index | `e2e/test_admin_login.py::test_admin_login_reaches_the_index` via `uv run --extra e2e pytest e2e -q` |
| `make e2e-down` kills the server, removes the pid file, frees port 8000 | the `make e2e-down` + `lsof` + `test ! -f` commands above |

## Risks

- **#14 (e2e layer costs more than it returns).** This is the phase where `e2e-up` stops being a
  no-op, so it is the phase where #14 is closed. Mitigations kept exactly as the register describes:
  `e2e-up` stays idempotent (early exit when the ready URL already answers), the browser install stays
  inside it, and the e2e suite stays *one* case — the login flow — not a second copy of what
  `tests/test_demo.py` proves. Every browser case is guarded by `server_available` (skip) and
  `importorskip("playwright.sync_api")`, so `pytest e2e` without a server or without the extra is a
  clean skip rather than a red suite, while the acceptance command runs both for real.
- **#20 (run stalls on an environment problem / a hung server holding port 8000).** T12 ends with
  `make e2e-down`, an `lsof` check on port 8000 and `docker ps`. `make demo`/`make demo-pg` are never
  started in this step because they block; the non-blocking PostgreSQL migrate+seed proves the same
  path and is followed by `make pg-down`.
- **#1 (we build the wrong product).** The demo is the instrument for this risk: the spec §13 manual
  QA walk-through goes into the README in T11 and its two most mechanical claims (three `/boom/` hits
  → one issue with count 3; `/storm/?n=5000` → < 1 s and ≤ 5 events) become assertions in
  `tests/test_demo.py` rather than waiting for Phase 10.
- **#7 (grouping wrong in practice).** `/boom/<int:n>/` and `/keyerror/<slug>/` are the human-visible
  probes for over/under-merging, and T7 pins both collapses as tests.
- **#2 (secret leak through a payload).** `/sensitive/` is the demo's scrubbing probe; its test
  asserts by *absence*, matching the register's rule. The template-side half of #2 stays with Phase 8.
- **#6 (flaky thread-transport tests).** Only one case in this phase uses the thread transport, and it
  uses `transaction=True` + `api.flush()` — no sleeps, no polling.
- **#16 (tests gamed/loosened).** The two skips introduced in `e2e/` are environment guards on an
  optional surface, not on the gate; `tests/test_demo.py` contains no skip.

## Out of scope

- **Admin UI (Phase 8):** `admin.py`, templates, templatetags, static files, `test_admin.py`, the
  `assertNumQueries` list/detail budgets, the screenshots in `docs/img/`, and every e2e case beyond
  login (issue list, detail, resolve → regressed). Until Phase 8 the demo's admin has no *Errors*
  section; the README "Try it" section says so explicitly.
- **Notifications and i18n (Phase 9):** the console email the spec §13 QA script mentions on a
  regression only arrives in Phase 9. `ADMINS` + the console email backend are configured now so that
  phase needs no demo change.
- **Release work (Phase 10):** README screenshots, the full settings table, the FAQ, the migration
  squash, the final two-backend manual QA run and the coverage gate.
- **Celery worker/beat in the demo:** eager execution only; no broker, no `docker-compose` worker
  service.
