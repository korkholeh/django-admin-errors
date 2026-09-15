# Profile: reusable Django app (library) — `django-admin-errors`

Corrected against reality at the architect step, 2026-09-15. Everything below is either verified on this
machine or is a contract Phase 0 must make true. Nothing here is aspirational.

**This is a library, not a site.** The generic "Django + htmx" profile the run was started with does not
apply: there is no `config/` project, no `apps/<app>/` tree, no `services.py`/`selectors.py` split, and
**no htmx** — the UI is Django admin templates plus ≤ 3 KiB of vanilla JS (ADR 0005). The htmx guidance
from the original profile has been deleted rather than adapted.

## Layout

```
pyproject.toml                 hatchling, src layout, deps = ["Django>=4.2"] only
Makefile                       thin wrappers only; uv is the real entry point
tox.ini                        the full matrix (see below)
.github/workflows/ci.yml       lint + sqlite matrix + postgres + celery + package jobs
.pre-commit-config.yaml        ruff
src/admin_errors/              the package — see docs/spec.md §4 for the full module list
  __init__.py                  __version__ ONLY; no import side effects
  apps.py conf.py checks.py models.py migrations/
  api.py capture.py context.py fingerprint.py handlers.py middleware.py
  writer.py storage.py retention.py routers.py signals.py notifications.py tasks.py
  integrations/celery.py
  admin.py templatetags/ templates/admin/admin_errors/ static/admin_errors/ locale/uk/
  management/commands/{errors_cleanup,errors_test,errors_stats}.py
tests/                         pytest; tests/settings.py is the minimal host
  settings.py conftest.py test_*.py
e2e/                           pytest-playwright specs, Phase 4 onward
benchmarks/bench_capture.py    not a test
demo/                          full Django project: manual QA, screenshots, PG target
  manage.py docker-compose.yml demo_project/ demo_app/
docs/spec.md  docs/dev/adr/  docs/img/
.autodev/                      run state — never shipped
```

Rules that follow from the layout:

- Tests import the **installed** package (`src/` layout), so a missing `package_data` entry fails in CI,
  not in a user's environment.
- `demo/` is never included in the wheel. It is the only place a `docker-compose.yml` lives.
- The package must work in a minimal host: only `django.contrib.{admin,auth,contenttypes,sessions,messages}`.
  Do not import `humanize`, `sites` or `staticfiles`-only APIs.

## Commands

All commands run from the repository root, non-interactively, with no venv activated. `uv` resolves and
installs the `dev` extra on first run.

| Key | Command |
|---|---|
| install | `uv sync --all-extras` |
| **test** (the gate the orchestrator runs between phases) | `uv run pytest -q` |
| test with coverage | `uv run pytest -q --cov=admin_errors --cov-report=term-missing` |
| test on PostgreSQL | `uv run pytest -q` with `DJANGO_DB=postgres` and `ADMIN_ERRORS_TEST_PG_URL` set |
| **lint** | `uv run ruff check . && uv run ruff format --check . && uv run python -m django check --settings=tests.settings && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings` |
| format | `uv run ruff format .` |
| full matrix | `uv run tox` |
| build | `uv run python -m build && uv run twine check dist/*` |
| **e2e up** | `make e2e-up` |
| **e2e** | `uv run --extra e2e pytest e2e -q` |
| **e2e down** | `make e2e-down` |
| demo (manual) | `make demo` |

`uv run pytest -q` is fixed by the intake and must not be changed. Phase 0 is responsible for making it
work: `pyproject.toml` declares the `dev` extra (pytest, pytest-django, ruff, coverage), and
`tests/settings.py` + `conftest.py` (with `DJANGO_SETTINGS_MODULE = tests.settings` in
`[tool.pytest.ini_options]`) make it work with no activated venv.

The lint command's last two segments are load-bearing: `django check` runs our own system checks
(W001–W003, E001–E002) against the test settings, and `makemigrations --check --dry-run` fails the build
when a model changed without a migration. Both are cheap and catch a whole class of silent breakage.

### Makefile contract (Phase 0 creates it; Phase 6 completes the demo targets)

The Makefile holds only what needs backgrounding or several steps. Every target is non-interactive.

- `make e2e-up` — **must be idempotent and must exit 0 in every phase.** Steps, in order:
  1. If `demo/manage.py` does not exist, print `e2e: demo/ not present yet, skipping` and exit 0.
     This is what makes the e2e step a free no-op in Phases 0–3.
  2. If `http://127.0.0.1:8000/admin/login/` already answers, print and exit 0 — do not fail on a taken port.
  3. `uv run python demo/manage.py migrate --noinput`
  4. `uv run python demo/manage.py demo_seed --issues 40 --days 30`  (creates superuser `admin`/`admin`)
  5. `uv run python -m playwright install chromium` — idempotent, a no-op once the browser is present.
  6. Start `uv run python demo/manage.py runserver 127.0.0.1:8000 --noreload` in the background, write its
     pid to `.autodev/e2e-server.pid`, and poll `http://127.0.0.1:8000/admin/login/` for up to 45 s.
     Exit non-zero only if it never comes up.
- `make e2e-down` — kill the pid in `.autodev/e2e-server.pid` if present, remove the file, exit 0 either
  way. Never leave a listening process behind.
- `make demo` / `make demo-pg` — the human path: migrate, seed, `runserver` in the **foreground**.
  `demo-pg` brings up the compose service first and sets `DEMO_DB=postgres`.
- `make test` / `make test-pg` / `make lint` / `make build` — thin wrappers around the `uv` commands above.

Ready URL for the orchestrator: `http://127.0.0.1:8000/admin/login/`. Superuser: `admin` / `admin`.
Never point the e2e commands at anything but this local demo project.

### PostgreSQL

SQLite is the per-phase gate. PostgreSQL gets a dedicated pass, no later than immediately after Phase 3,
and again before the run ends. Verified present on this machine: Docker 29.7.2 (running) and
`psql` 17.4 (Postgres.app). Prefer the reproducible compose service:

```
docker compose -f demo/docker-compose.yml up -d
ADMIN_ERRORS_TEST_PG_URL=postgres://postgres:postgres@localhost:5432/admin_errors_test DJANGO_DB=postgres uv run pytest -q
docker compose -f demo/docker-compose.yml down
```

Code written before that pass must already honour the PostgreSQL rules: every racing create inside its
own `atomic()` savepoint, and no `select_for_update` anywhere.

## Verified toolchain on this machine (2026-09-15)

`uv 0.11.21` · `python3 3.13.3` · `ruff 0.16.7` · `docker 29.7.2` (daemon running, no containers) ·
`psql 17.4` · `git 2.50.1` · `make 3.81` (GNU make 3.81 — **BSD-era; do not use `.ONESHELL`, `$(shell …)`
tricks that need make 4.x, or `:=` with `$(file …)`**) · `node 22.23.1` · `npx 10.9.8`.

`uv` will fetch the other interpreters (3.10–3.12) for tox; only 3.13 is present as a system Python.

## Support matrix

Verified on djangoproject.com on 2026-09-15: currently supported series are **5.2 LTS, 6.0, 6.1**.
Django **4.2 is EOL upstream** (extended support ended April 2026) but spec §3 fixes it as the floor, so
it stays as a best-effort, CI-tested target.

| Python | Django |
|---|---|
| 3.10 | 4.2, 5.2 |
| 3.11 | 4.2, 5.2 |
| 3.12 | 4.2, 5.2, 6.0, 6.1 |
| 3.13 | 5.2, 6.0, 6.1 |

Plus tox envs: `lint`, `postgres` (py313 × dj52, dj61), `celery`, `package`. Series are pinned
(`Django>=6.1,<6.2`), patch releases are not. Never upper-pin Django in `pyproject.toml` itself.

## Conventions

- Zero runtime dependencies beyond Django. Celery, psycopg, pytest, ruff and playwright are extras.
- ruff lint + format, line length 100. Type hints on all public functions. mypy is not a gate.
- English for code, identifiers, comments, docs and commit messages. UI strings go through
  `{% translate %}` / `gettext_lazy`, with a Ukrainian catalogue.
- Commits: `feat(scope): …`, `fix(scope): …`, `test(scope): …`, `docs: …`. One commit per logical unit.
  The orchestrator commits — agents never run `git commit`.
- Every phase ends with: ruff clean, `uv run pytest -q` green on SQLite, a `CHANGELOG.md` entry under
  *Unreleased*. Never start the next phase with a red suite; never skip a test "for later".
- Settings are read through `admin_errors.conf.settings`, never from `django.conf.settings.ADMIN_ERRORS`
  directly — the proxy is what makes `override_settings` work in tests.
- Default transport in tests is `TRANSPORT="sync"`. Thread tests opt in explicitly and need
  `transaction=True`.

## Pitfalls specific to this project

- **`AppConfig.ready()` must not** query the database, start a thread, or touch the filesystem. The writer
  thread starts lazily on the first `enqueue()` — this is what makes the `runserver` autoreloader and
  gunicorn `--preload` work.
- **`src/admin_errors/__init__.py` holds `__version__` only.** Any import side effect there risks
  `AppRegistryNotReady` in a host project.
- **`TestCase` wraps each test in a transaction invisible to other connections.** A thread-transport test
  that does not set `transaction=True` will appear to lose data. If such a test is flaky on SQLite,
  switch it to a file-based test DB via `TEST["NAME"]` — never add a sleep, never skip it.
- **PostgreSQL aborts the whole transaction on `IntegrityError`.** Any create that can race must be inside
  its own `atomic()` savepoint, or every following query in that transaction fails.
- **SQLite ignores `select_for_update`.** Do not use it anywhere; the select-then-update-then-savepointed-insert
  pattern does not need it.
- **N+1 hides in admin templates.** The 14-day sparkline over 50 rows is the obvious trap. Use one filtered
  `Prefetch` and assert with `assertNumQueries` (list ≤ 12, detail ≤ 15).
- **A permission on a view is not a permission on a template include.** Gate `view_issue_context` in both,
  including the "Copy as text" traceback, and test that the sensitive strings are *absent* from the response.
- **`DEBUG=True` hides real 500s behind the technical error page.** The e2e/demo run must exercise the real
  paths; `CAPTURE_IN_DEBUG` defaults to `True` so capture itself still works either way.
- **`propagate: False` on `django.request`** in a host's `LOGGING` silently disables view-exception capture.
  That is what check W002 exists for.
- **`DummyCache` makes `cache.add()` always succeed**, so the opportunistic-cleanup lock degrades to the
  process-local timestamp. Accepted; cleanup is idempotent.
- **Never store settings or environment** in a payload, unlike Django's debug page.
- **Do not leave background processes behind.** If a step starts the demo server, that step runs
  `make e2e-down` before finishing.

## End-to-end

Driver: Playwright via `pytest-playwright` (the `e2e` extra), against the demo project the Makefile
started. Meaningful only from **Phase 4** onward — earlier phases have no user-facing surface and
`make e2e-up` no-ops.

Cases worth having, taken from the manual QA script in spec §13 rather than duplicating `test_admin.py`:

- log in as `admin`/`admin`, open the issue list, assert the summary cards and at least one sparkline render;
- hit `/boom/` three times, reload the list, assert one issue with count 3;
- hit `/boom/1/` and `/boom/2/`, assert they collapse into a single issue (message normalisation, visible);
- open an issue detail: traceback renders, a library frame is collapsed, the locals toggle works, the
  chained-exception separator appears on `/nested/`;
- resolve the issue from the detail page, hit `/boom/` again, assert the **regressed** badge;
- a non-superuser with `view_issue` only: assert the request-context section is absent and a POST to the
  status URL returns 403;
- toggle the browser to dark mode and assert the page still meets contrast expectations visually
  (screenshot).

The browser pass also produces the README screenshots into `docs/img/`: issue list and issue detail, in
light and dark.
