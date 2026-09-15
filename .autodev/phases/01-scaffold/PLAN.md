# Phase 1 — Scaffold and toolchain

Goal: a clean checkout builds, lints, tests and runs the e2e harness with every command from
`.autodev/PROFILE.md` working from the repository root, with no venv activated.

## Context

What exists today (verified with `find`, 2026-09-15):

- `CLAUDE.md`, `.gitignore` (already complete for Python/Django/tox/coverage/node).
- `docs/spec.md`, `docs/dev/adr/0001…0008`.
- `.autodev/` run state: `PROFILE.md`, `ARCHITECTURE.md`, `RISKS.md`, `ROADMAP.md`, `DECISIONS.md`,
  `PROGRESS.md`, `guides/`.
- **No** `pyproject.toml`, no `src/`, no `tests/`, no `e2e/`, no `tox.ini`, no `Makefile`, no
  `.github/`, no `README.md`, no `LICENSE`, no `CHANGELOG.md`. There is no Python code in the
  repository at all, so this phase is pure creation — nothing to migrate, nothing to keep working.

What this phase changes: it creates the whole toolchain skeleton and an importable but behaviour-free
`admin_errors` package. No models, no capture, no admin, no settings proxy — those are Phases 2–3.

Key files created (nothing is modified except `.gitignore`, and that only if a gap shows up):

```
pyproject.toml            hatchling, src layout, deps = ["Django>=4.2"], extras dev/e2e/celery/postgres
tox.ini                   11-cell sqlite matrix + lint, postgres, celery, package
Makefile                  PROFILE.md contract (GNU make 3.81 compatible — no .ONESHELL)
.pre-commit-config.yaml   ruff check + ruff format
.github/workflows/ci.yml  lint, sqlite-matrix, postgres, celery, package
README.md LICENSE CHANGELOG.md
src/admin_errors/__init__.py      __version__ = "0.1.0.dev0", nothing else
src/admin_errors/apps.py          AdminErrorsConfig, verbose_name "Errors", empty ready()
src/admin_errors/migrations/__init__.py
tests/settings.py tests/package_settings.py tests/conftest.py tests/test_scaffold.py
e2e/conftest.py e2e/test_harness.py
```

## Design

### Module layout

Exactly the spec §4 tree, but only the two modules this phase owns are created; the rest arrive in
their own phases. `src/` layout means the test run imports the *installed* package, so a missing
`package_data` entry fails here rather than in a user's environment (risk #17).

`src/admin_errors/__init__.py` holds `__version__ = "0.1.0.dev0"` and a module docstring — no
imports, no `default_app_config`, no logging setup. Any import side effect risks `AppRegistryNotReady`
in a host project (CLAUDE.md pitfall), and hatchling reads the version out of this file, so it must be
importable with no Django settings configured.

`src/admin_errors/apps.py`:

```python
class AdminErrorsConfig(AppConfig):
    name = "admin_errors"
    label = "admin_errors"
    verbose_name = _("Errors")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Nothing yet: Phase 2 registers checks, Phase 3 installs the handler."""
```

`ready()` stays a no-op body with the docstring that says what may and may not go in it: no DB query,
no thread, no filesystem access — ever (ADR 0002, CLAUDE.md pitfall). `default_auto_field` is set now
so Phase 2's single migration is generated with `BigAutoField` from the start and never needs a
follow-up migration (risk #19).

`src/admin_errors/migrations/__init__.py` is created empty now so the app is migration-ready and
`makemigrations admin_errors --check --dry-run` has a stable target from the first lint run.

### `pyproject.toml`

- `[build-system]` hatchling; `[project] name = "django-admin-errors"`,
  `dynamic = ["version"]` with `[tool.hatch.version] path = "src/admin_errors/__init__.py"`, so
  `__version__` is the single source of truth (ARCHITECTURE → Delivery).
- `requires-python = ">=3.10"`, `dependencies = ["Django>=4.2"]` — never upper-pinned (PROFILE).
- Classifiers for Python 3.10–3.13 and Django 4.2/5.2/6.0/6.1, MIT licence, `Framework :: Django`.
- Extras: `dev` = pytest, pytest-django, pytest-cov, coverage, ruff, build, twine, tox, tox-uv;
  `e2e` = pytest-playwright; `celery` = `celery>=5`; `postgres` = `psycopg[binary]>=3.1`.
  Base install keeps exactly one dependency.
- `[tool.hatch.build.targets.wheel] packages = ["src/admin_errors"]` plus an explicit
  `artifacts = ["src/admin_errors/locale/**/*.mo", "src/admin_errors/static/**", "src/admin_errors/templates/**"]`
  so compiled catalogues (git-ignored build products) and the asset trees are in the wheel by
  declaration, not by hatchling's default-include luck. `[tool.hatch.build.targets.sdist]` includes
  `src/`, `tests/`, `tox.ini`, `README.md`, `CHANGELOG.md`, `LICENSE` and excludes `.autodev/`,
  `demo/`, `e2e/`, `docs/img/`.
- `[tool.ruff] line-length = 100, target-version = "py310"`; lint rules `E,F,W,I,UP,B,C4,DJ,RUF`.
  No per-file ignores beyond `tests/settings.py` needing none. Never disable a rule to hide a finding.
- `[tool.pytest.ini_options]`: `DJANGO_SETTINGS_MODULE = "tests.settings"`, `testpaths = ["tests"]`,
  `addopts = "-ra --strict-markers --strict-config"`, `pythonpath = ["."]` so `tests.settings` resolves
  with no venv activated and no `PYTHONPATH` export.
  `testpaths = ["tests"]` is what keeps the per-phase gate (`uv run pytest -q`) from collecting `e2e/`,
  which needs the `e2e` extra; the e2e command passes the `e2e` path explicitly.
- `[tool.coverage.run] source = ["admin_errors"], branch = true`; no `exclude_lines` for capture or
  storage paths (risk #16). The `--cov-fail-under=90` threshold lives in the CI invocation, not in
  `addopts`, so a coverage dip never masquerades as a functional failure in the phase gate.

### `tests/settings.py` — the minimal host

`INSTALLED_APPS` is exactly `django.contrib.{contenttypes,auth,sessions,messages,admin}` +
`admin_errors`. No `staticfiles`, no `sites`, no `humanize` — the spec §3 minimal-host promise is
enforced by the test settings themselves, and a test asserts the list so a later phase cannot quietly
add a contrib app to make something work.

Database selection is a small pure helper so it is testable without a server:

```python
def database_from_url(url: str) -> dict[str, object]:  # postgres:// → Django DATABASES entry
```

`DJANGO_DB` (`sqlite`, the default, or `postgres`) picks the backend; for `postgres` the URL comes from
`ADMIN_ERRORS_TEST_PG_URL`, defaulting to the compose DSN
`postgres://postgres:postgres@localhost:5432/admin_errors_test`. SQLite uses `:memory:`. The helper
parses with `urllib.parse.urlsplit`, unquotes the password, and raises `ValueError` on a non-postgres
scheme rather than silently producing a broken config.

Also set: `USE_TZ = True`, `TIME_ZONE = "UTC"`, `SECRET_KEY` a fixed test constant,
`DEFAULT_AUTO_FIELD`, a minimal `TEMPLATES` with the admin context processors, `ROOT_URLCONF =
"tests.urls"` is **not** created yet (no views this phase) — `ROOT_URLCONF` points at a module-level
`urlpatterns = []` defined inline via `ROOT_URLCONF = "tests.settings"`, the standard trick for a
view-less host; Phase 3 replaces it with `tests/urls.py`. `MIDDLEWARE` carries only what the admin
needs. `ADMIN_ERRORS = {"TRANSPORT": "sync"}` is set now so the default test transport is fixed from
the first phase (PROFILE convention), even though nothing reads it until Phase 2.

`tests/package_settings.py` is a second, dependency-free settings module (no pytest import) used by the
`package` tox env and the CI package job to run `django-admin check` and `migrate` against a file-backed
SQLite database from a clean venv containing only the built wheel + Django.

`tests/conftest.py` holds a `repo_root` fixture (`pathlib.Path` of the repository root, from
`__file__`) used by the subprocess import test, and nothing else. Phase 3 grows it.

### `e2e/` harness

`e2e/conftest.py` defines:

- `E2E_BASE_URL` / `ready_url()` — `http://127.0.0.1:8000` and `/admin/login/`, overridable through
  `ADMIN_ERRORS_E2E_BASE_URL` (the orchestrator's ready URL, PROFILE.md).
- `server_available` — a session fixture that does one `urllib.request.urlopen` with a 2 s timeout and
  calls `pytest.skip("no demo server on …; run make e2e-up")` on `URLError`/`OSError`. This is an
  environment gate, not a loosened test: from Phase 7 the demo exists, `make e2e-up` starts it, and the
  same fixture lets the real browser cases run. The roadmap sanctions this shape explicitly.
- No module-level `playwright` import in Phase 1, so the file is readable even without the extra.

`e2e/test_harness.py` carries one **unconditionally passing** case (the harness contract: base URL
parses, ready path is `/admin/login/`, `admin_errors` is importable from the e2e environment) plus one
case that consumes `server_available` and asserts the login page answers `200` and contains a password
field — green after `make e2e-up` from Phase 7, skipped before that.

### `Makefile` (GNU make 3.81 — BSD-era)

No `.ONESHELL`, no make-4 `$(file …)`, no `:=` cleverness. Every recipe is a single `&&`/`;`-joined
shell line or a small `if`-block written with explicit `\` continuations. Targets, per PROFILE:

- `e2e-up`: (1) `test -f demo/manage.py || { echo "e2e: demo/ not present yet, skipping"; exit 0; }`;
  (2) if the ready URL already answers, print and exit 0; (3) `migrate --noinput`; (4)
  `demo_seed --issues 40 --days 30`; (5) `python -m playwright install chromium`; (6) background
  `runserver 127.0.0.1:8000 --noreload`, write the pid to `.autodev/e2e-server.pid`, poll the ready URL
  for ≤ 45 s, exit non-zero only if it never comes up. Steps 3–6 are written now but are unreachable
  until `demo/manage.py` exists; Phase 7 is the first phase that executes them.
- `e2e-down`: kill the pid file's process if present, remove the file, `exit 0` either way.
- `test`, `test-pg`, `lint`, `build`: thin wrappers around the exact `uv` commands in PROFILE.md.
- `demo`, `demo-pg`: placeholders that print "demo/ arrives in Phase 7" and exit 0 — they are not part
  of any acceptance criterion this phase, and a placeholder that exits 0 cannot be mistaken for a
  working demo because it prints why.
- `.PHONY` for every target; `SHELL := /bin/sh`.

Reachability is probed with `python -c` + `urllib` rather than `curl`, so the Makefile depends on
nothing that is not already required.

### `tox.ini`

`requires = tox>=4.11, tox-uv>=1.11`; `runner = uv-venv-runner` so `uv` provides and, where missing,
downloads the 3.10–3.12 interpreters (only 3.13 is a system Python on this machine, PROFILE).

`env_list` is exactly the legal cells from the support matrix:
`py{310,311}-dj{42,52}-sqlite`, `py312-dj{42,52,60,61}-sqlite`, `py313-dj{52,60,61}-sqlite`
(11 cells), plus `lint`, `py313-dj{52,61}-postgres`, `celery`, `package`. Django is pinned per series
(`Django>=6.1,<6.2`); patch releases float. `commands = pytest {posargs:-q}` so CI can append coverage
flags. `lint` runs the four-segment lint command. `postgres` sets `DJANGO_DB=postgres` and passes
`ADMIN_ERRORS_TEST_PG_URL` through. `celery` installs the `celery` extra and runs
`pytest tests/test_celery.py` — that file does not exist until Phase 5, so in this phase the env's
command is `pytest -q -m celery` over an empty marker selection… which would exit 5. Instead the
`celery` env runs the full suite with `celery` installed (`pytest {posargs:-q}`), which is a real check
today (the guarded import must not break with celery present) and narrows to `tests/test_celery.py`
in Phase 5. `package` builds, `twine check`s, installs the wheel into a fresh venv and runs
`django-admin check` + `migrate` against `tests.package_settings` with `--pythonpath .`.

### `.github/workflows/ci.yml`

Five jobs, all on `ubuntu-latest`, all driven through `uv` + tox so the matrix is defined once in
`tox.ini` and merely *listed* in the workflow:

| Job | What it runs |
|---|---|
| `lint` | `uv run tox -e lint` |
| `sqlite-matrix` | `matrix.include` with the 11 Python×Django cells → `uv run tox -e py{py}-dj{dj}-sqlite -- -q --cov=admin_errors --cov-report=term-missing --cov-fail-under=90` |
| `postgres` | `services: postgres:16`, `DJANGO_DB=postgres`, `ADMIN_ERRORS_TEST_PG_URL=…` → `uv run tox -e py313-dj52-postgres,py313-dj61-postgres` |
| `celery` | `uv run tox -e celery` |
| `package` | `uv run tox -e package`, uploads `dist/*` as an artifact |

Triggers: `push` on the run's branches and `main`, `pull_request`, `workflow_dispatch`.
`concurrency` cancels superseded runs. No secrets, no upload to PyPI (intake).

### Error handling

The only runtime error paths this phase owns are tooling ones and they all fail loudly: the settings
URL parser raises `ValueError` on a bad DSN instead of building a half-config; `make e2e-up` exits
non-zero only when a server it started never became ready; `make e2e-down` never fails. Nothing in
`src/admin_errors/` can raise — there is no code there yet beyond a constant and a class body.

### How this honours the architecture

Zero runtime dependencies (one `dependencies` entry), `src/` layout + hatchling + MIT, version from
`__init__.py`, side-effect-free `__init__`, `ready()` that does nothing, the exact verified support
matrix, SQLite as the per-phase gate with PostgreSQL selected by env var, coverage enforced in CI only,
and the Makefile contract reproduced verbatim from PROFILE.md.

Deviations from `.autodev/ARCHITECTURE.md` / spec, both appended to `DECISIONS.md`:

1. The `package` env runs `django-admin check` + `migrate` against `tests/package_settings.py`, not
   "the demo settings" (spec §14.1): `demo/` does not exist until Phase 7, and a packaging job that is
   red for six phases teaches the run to ignore it. Phase 7 may repoint it at the demo settings.
2. `tox-uv` is added to the `dev` extra (dev-only, so the zero-runtime-dependency promise is untouched)
   because PROFILE.md assumes `uv` fetches the 3.10–3.12 interpreters for tox and plain tox will not.

## Tasks

- [x] T1: `pyproject.toml` — build system, project metadata, `dependencies = ["Django>=4.2"]`, the four
      extras, hatchling wheel/sdist config with explicit template/static/locale artifacts,
      `[tool.ruff]` (line-length 100), `[tool.pytest.ini_options]`
      (`DJANGO_SETTINGS_MODULE=tests.settings`, `testpaths=["tests"]`, `pythonpath=["."]`),
      `[tool.coverage.*]`.
- [x] T2: `src/admin_errors/__init__.py` (`__version__ = "0.1.0.dev0"` + docstring, no imports),
      `src/admin_errors/apps.py` (`AdminErrorsConfig`, `verbose_name` "Errors", `default_auto_field`,
      empty documented `ready()`), `src/admin_errors/migrations/__init__.py`.
- [x] T3: `tests/settings.py` — minimal host, `database_from_url()` helper, `DJANGO_DB` /
      `ADMIN_ERRORS_TEST_PG_URL` selection, `ADMIN_ERRORS = {"TRANSPORT": "sync"}`, inline empty
      `urlpatterns`; `tests/package_settings.py` (pytest-free, file-backed SQLite);
      `tests/conftest.py` (`repo_root` fixture).
- [x] T4: `tests/test_scaffold.py` — the phase's unit cases:
      (a) `__version__ == "0.1.0.dev0"`;
      (b) `apps.get_app_config("admin_errors")` has `verbose_name == "Errors"` and
          `default_auto_field == "django.db.models.BigAutoField"`;
      (c) subprocess `python -c "import admin_errors; print(admin_errors.__version__)"` with
          `DJANGO_SETTINGS_MODULE` cleared exits 0 and prints the version — proves import is
          side-effect free and settings-independent;
      (d) `settings.INSTALLED_APPS` equals the minimal-host list exactly;
      (e) `database_from_url` maps `postgres://u:p%40ss@h:5433/db` to
          engine/NAME/USER/PASSWORD/HOST/PORT correctly and raises `ValueError` on `mysql://…` and on a
          scheme-less string.
- [x] T5: `e2e/conftest.py` — `E2E_BASE_URL`, ready-URL helper, `server_available` session fixture that
      skips on `URLError`/`OSError` with an actionable message; no top-level playwright import.
- [x] T6: `e2e/test_harness.py` — one unconditional passing harness-contract case and one
      `server_available` case asserting the login page answers 200 with a password field.
- [x] T7: `Makefile` — `e2e-up` (skip-and-exit-0 without `demo/manage.py`; already-listening short
      circuit; migrate → seed → playwright install → backgrounded `runserver` + pid file + 45 s poll),
      `e2e-down`, `test`, `test-pg`, `lint`, `build`, `demo`/`demo-pg` placeholders, `.PHONY`,
      make-3.81-safe syntax only.
- [x] T8: `tox.ini` — `tox-uv` runner, the 11 sqlite cells, `lint`, two `postgres` cells, `celery`,
      `package` (build + `twine check` + clean-venv wheel install + `django-admin check`/`migrate`
      against `tests.package_settings`).
- [x] T9: `.github/workflows/ci.yml` — `lint`, `sqlite-matrix` (`matrix.include` listing exactly the 11
      cells, coverage `--cov-fail-under=90`), `postgres` (postgres:16 service), `celery`, `package`
      (artifact upload); triggers + `concurrency`.
- [x] T10: `.pre-commit-config.yaml` (ruff check `--fix` + ruff format, pinned to the installed ruff
      version), `README.md` stub (what it is, install, compatibility table incl. the 4.2 best-effort
      note for risk #10, links to spec/CHANGELOG), `LICENSE` (MIT, 2026 Oleh Korkh),
      `CHANGELOG.md` with `## [Unreleased]` + an `### Added` entry for this scaffold; check
      `.gitignore` covers `dist/`, `build/`, `.tox/`, `*.egg-info/` (it does) and add nothing else.
- [x] T11: Run the whole verification table below end to end, including `make e2e-down` and a port-8000
      check; fix the product, never the check.
- [x] T12: Append the two deviations from *Design* plus any judgement calls to `.autodev/DECISIONS.md`
      under `## p01-plan`, and confirm `test_command` is unchanged.

## Verification

Commands, in order, from the repository root with no venv activated:

```sh
uv sync --all-extras
uv run pytest -q
uv run ruff check . && uv run ruff format --check . \
  && uv run python -m django check --settings=tests.settings \
  && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings
make e2e-up && uv run --extra e2e pytest e2e -q && make e2e-down
uv run python -c "import admin_errors; print(admin_errors.__version__)"
uv run python -m build && uv run twine check dist/*
uv run tox -e lint
uv run python -m pytest -q --cov=admin_errors --cov-report=term-missing --cov-fail-under=90
lsof -nP -iTCP:8000 -sTCP:LISTEN || echo "port 8000 free"
```

| Acceptance criterion | What proves it |
|---|---|
| `uv run pytest -q` exits 0 on a clean checkout, ≥ 1 passing test | `uv run pytest -q` → 7 passed from `tests/test_scaffold.py` (T4 a–e) |
| Full four-segment lint command exits 0 | the lint block above; `django check` needs T3's settings to import, `makemigrations --check` needs T2's `migrations/` package |
| `make e2e-up` exits 0 printing that `demo/` is absent | `make e2e-up` prints `e2e: demo/ not present yet, skipping`, `echo $?` → 0 (T7 step 1) |
| `uv run --extra e2e pytest e2e -q` exits 0 | `e2e/test_harness.py::test_harness_contract` passes; the `server_available` case skips with its reason (T5, T6) |
| `make e2e-down` exits 0, nothing listening on 8000 | `make e2e-down`; `lsof -nP -iTCP:8000 -sTCP:LISTEN` reports nothing |
| `python -m build` + `twine check dist/*` exit 0, wheel **and** sdist | `ls dist/` shows `django_admin_errors-0.1.0.dev0-py3-none-any.whl` and `django_admin_errors-0.1.0.dev0.tar.gz`; `twine check` → PASSED (T1) |
| `uv run tox -e lint` exits 0 | same, via T8's `lint` env |
| `import admin_errors` prints `0.1.0.dev0` with no Django settings configured | `uv run python -c …` **and** `tests/test_scaffold.py::test_import_has_no_side_effects` (T4c), which runs it in a subprocess with `DJANGO_SETTINGS_MODULE` removed |
| `ci.yml` defines lint / sqlite-matrix / postgres / celery / package, matrix = exactly the PROFILE cells | `uv run python -c` reading `ci.yml` is not available (no yaml runtime dep); verified by reading the file and by cross-checking `tox.ini`'s `env_list` against ARCHITECTURE's matrix table — 11 cells: py310{42,52} py311{42,52} py312{42,52,60,61} py313{52,60,61} |

Coverage: with only `__init__.py` and `apps.py` in the package, T4's cases import both, so
`--cov-fail-under=90` is met on real coverage, not on exclusions.

## Risks

Rows of `RISKS.md` this phase touches:

- **#10 (Django 4.2 EOL but the spec's floor)** — kept: `dependencies = ["Django>=4.2"]`, 4.2 cells in
  `tox.ini` and the CI matrix, and a README compatibility note that 4.2 is CI-tested best-effort while
  Django's own security support is the host's responsibility. Opens the row's Phase-0 half.
- **#16 (coverage gamed / suite loosened)** — `--cov-fail-under=90` is in CI from this phase, coverage
  config carries no `exclude_lines`, and `--strict-markers --strict-config` stop a stray marker from
  silently deselecting tests. The one conditional skip in `e2e/` is an environment gate on a case that
  runs for real from Phase 7, and it sits beside an unconditional passing case so the e2e suite can
  never be all-skipped and still look green.
- **#17 (packaging defect: templates/static/locale missing from the wheel)** — `src/` layout so tests
  import the installed package, explicit `artifacts` for the three asset trees, and the `package` env /
  CI job that installs the wheel into a clean venv and runs `django-admin check` + `migrate`. The job
  exists and is green from this phase; it becomes load-bearing when Phase 8 adds templates.
- **#19 (migration churn)** — `makemigrations admin_errors --check --dry-run` is the last segment of the
  lint command from the first phase, `migrations/` exists from the start, and `default_auto_field` is set
  now so Phase 2's single migration is not followed by an auto-field fixup.
- **#14 / #20 (e2e cost, stalled run, held port)** — `e2e-up` no-ops without `demo/manage.py` and short
  circuits when port 8000 already answers; `e2e-down` always exits 0; the pid file lives at
  `.autodev/e2e-server.pid`; `--noreload` means one pid to kill. T11 verifies the port is free at the end.
- **#15 (scope creep)** — this plan creates two Python modules and no behaviour. No settings proxy, no
  models, no capture, no admin, no demo.

## Out of scope

- `conf.py`, `checks.py`, `models.py`, the initial migration and `fingerprint.py` — Phase 2.
- `context.py`, `capture.py`, `api.py`, `handlers.py`, `middleware.py`, `storage.py`, `tests/urls.py` —
  Phase 3.
- `writer.py`, `signals.py`, sampling/admission, `benchmarks/bench_capture.py` — Phase 4.
- `retention.py`, `routers.py`, `tasks.py`, `integrations/celery.py`, management commands and the
  narrowed `celery` tox env command — Phase 5.
- `demo/docker-compose.yml` and the actual PostgreSQL pass — Phase 6.
- `demo/` (`manage.py`, `demo_project/`, `demo_app/`, `demo_seed`) and the working `make demo` /
  `make demo-pg` — Phase 7; the placeholders created here are replaced there.
- Browser e2e cases, `admin.py`, templates, static assets — Phase 8.
- `notifications.py`, i18n catalogue — Phase 9.
- README screenshots, the full README, the migration squash, the `0.1.0` version bump and the
  clean-venv install proof — Phase 10.
