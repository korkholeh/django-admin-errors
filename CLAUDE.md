# django-admin-errors

Reusable Django app (import name `admin_errors`) that records unhandled exceptions and error-level
log records into the host project's own database and shows them Sentry-style inside the Django
admin: grouped into issues by fingerprint, deduplicated, with traceback, request context, counts and
14/30-day trends. Target: projects where Sentry is overkill or not allowed.

This is a **library, not a site**: no `config/` project, no htmx — the UI is admin templates + vanilla JS.

## Stack

Python 3.10–3.13 · Django 4.2 / 5.2 / 6.0 / 6.1 · SQLite (JSON1) + PostgreSQL ≥ 13 ·
zero runtime dependencies beyond Django · hatchling, `src/` layout · pytest + pytest-django · ruff ·
tox · pytest-playwright (e2e) · Celery ≥ 5 optional, imported guardedly.

## Commands

All commands run from the repository root, non-interactively, with no venv activated.

| What | Command |
|---|---|
| install | `uv sync --all-extras` |
| **test** (the gate) | `uv run pytest -q` |
| test + coverage | `uv run pytest -q --cov=admin_errors --cov-report=term-missing` |
| test on PostgreSQL | `DJANGO_DB=postgres ADMIN_ERRORS_TEST_PG_URL=postgres://postgres:postgres@localhost:5432/admin_errors_test uv run pytest -q` |
| test on PostgreSQL (compose) | `make test-pg` — brings up `demo/docker-compose.yml`'s `postgres:16` service, runs the suite, always tears it down |
| bring up / down the PG container | `make pg-up` / `make pg-down` |
| **lint** | `uv run ruff check . && uv run ruff format --check . && uv run python -m django check --settings=tests.settings && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings` |
| format | `uv run ruff format .` |
| benchmark | `uv run python benchmarks/bench_capture.py` |
| full matrix | `uv run tox` |
| build | `uv run python -m build && uv run twine check dist/*` |
| **e2e** | `make e2e-up && uv run --extra e2e pytest e2e -q && make e2e-down` |
| demo (manual) | `make demo` (SQLite) · `make demo-pg` (compose Postgres) |
| i18n catalogue | `cd src/admin_errors && PYTHONPATH=<repo root> uv run python -m django makemessages -l uk --settings=tests.settings`, translate new `msgid`s, then `... compilemessages` |

`make e2e-up` is idempotent and exits 0 when `demo/` does not exist yet, or when the server already
answers. Otherwise it migrates, seeds `--reset` (superuser `admin`/`admin`), installs chromium,
backgrounds `runserver 127.0.0.1:8000 --noreload` with its pid in `.autodev/e2e-server.pid`, and
polls `http://127.0.0.1:8000/admin/login/`. `make e2e-down` always kills it. Never leave a listening
process behind.

## Layout

```
src/admin_errors/   the package (see docs/spec.md §4 for the module list)
tests/              pytest; tests/settings.py is the minimal host
e2e/                pytest-playwright specs against demo/
benchmarks/         bench_capture.py, not a test
demo/               full Django project: manual QA, screenshots, PG target; never in the wheel
docs/spec.md  docs/dev/architecture.md  docs/dev/adr/  docs/img/
docs/user/ (operator-facing docs, task-shaped)
```

`tox -e package` ends by running `tests/package_smoke.py` with the clean venv's own interpreter,
proving templates, static files and the `uk` catalogue are actually present and render inside the
installed wheel, not just that it imports.

## Conventions

- Zero runtime deps beyond Django. Celery, psycopg, pytest, ruff, playwright are extras.
- ruff lint + format, line length 100. Type hints on all public functions. mypy is not a gate.
- English for code, identifiers, comments, docs and commit messages. UI strings go through
  `{% translate %}` / `gettext_lazy`, with a Ukrainian catalogue.
- Commits `feat(scope): …` / `fix(scope): …` / `test(scope): …` / `docs: …`, one per logical unit.
- Settings are read through `admin_errors.conf.settings`, never from `django.conf.settings` directly —
  the proxy is what makes `override_settings` work in tests.
- Default transport in tests is `TRANSPORT="sync"`; thread tests opt in and need `transaction=True`.
- `NOTIFY_BACKEND`'s signal receivers are connected/disconnected dynamically
  (`notifications.refresh_connections()`, from `ready()` and on `setting_changed`), never always-on —
  this keeps `storage._fire`'s `has_listeners()` check truthful so the `test_storage.py` query
  budgets never move when no one is listening.
- Every phase ends with ruff clean, `uv run pytest -q` green on SQLite, a `CHANGELOG.md` entry under
  *Unreleased*. Never start a phase with a red suite; never skip a test "for later".

## Pitfalls

- `AppConfig.ready()` must not query the DB, start a thread, or touch the filesystem. The writer
  thread starts lazily on the first `enqueue()` (`runserver` autoreloader, gunicorn `--preload`).
- `src/admin_errors/__init__.py` holds `__version__` only — no import side effects.
- PostgreSQL aborts the whole transaction on `IntegrityError`: every racing create goes inside its
  own `atomic()` savepoint. SQLite ignores `select_for_update` — do not use it anywhere.
- Capture must never raise and never recurse: whole pipeline in `try/except BaseException`
  (re-raise `KeyboardInterrupt`/`SystemExit`), thread-local recursion guard, safe `repr()`.
- Scrubbing is delegated to Django's `get_exception_reporter_filter(request)` — never hand-rolled.
  A permission on a view is not a permission on a template include: gate `view_issue_context` in
  both, and test that the sensitive strings are *absent* from the response.
- N+1 hides in admin templates (14-day sparkline × 50 rows). One filtered `Prefetch`;
  `assertNumQueries` bounds: list ≤ 12, detail ≤ 15.
- The fingerprint algorithm is a frozen contract pinned by golden-value tests (ADR 0003).
- Exactly one migration ships (`0001_initial`, pinned by `tests/test_docs.py`). 0.1.0 needed no
  squash — only one was ever created. Everything after 0.1.0 must be additive.
- Never store settings or environment in a payload, unlike Django's debug page.
- `e2e/` shares one long-lived demo server, and sampling budgets are process-global per fingerprint.
  A suite that re-hits `/boom/` from many files drains that budget. Give a new e2e case its own demo
  URL rather than reusing a shared one, and restart the server (`make e2e-down && make e2e-up`)
  before trusting a red run.

Docs: `docs/dev/architecture.md` (how it fits together, and where it diverged from the design of
record), `docs/dev/adr/` (the eight accepted decisions), `docs/user/` (operator guide),
`docs/spec.md` (the implementation spec everything was built from).

Autodev docs: .autodev/ (HANDOFF.md, ARCHITECTURE.md, RISKS.md, ROADMAP.md, PROGRESS.md, DECISIONS.md, phases/NN-*/PLAN.md)
