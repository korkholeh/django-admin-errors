# Intake — django-admin-errors

- **Date:** 2026-09-15
- **Spec:** docs/spec.md
- **Profile:** django-htmx (reusable Django app + Django admin templates; no htmx/JS framework — the profile is the closest fit for "Django + server-rendered templates")

## Answers

- **Test command:** `uv run pytest -q`, run from the repo root. The repository is empty, so this is a
  *contract Phase 0 must satisfy*: `pyproject.toml` must declare the `dev` extra (pytest, pytest-django,
  ruff) and `tests/settings.py` + `conftest.py` must make this command work with no activated venv.
  The spec's `make test` target (Section 13) may exist as a thin wrapper, but `uv run pytest -q` is the
  command the orchestrator runs between phases.

- **PostgreSQL:** SQLite is the per-phase gate. PostgreSQL is **not** run on every phase; instead it gets
  its own pass once the storage layer is stable (after Phase 3 at the latest, and again before the run
  finishes). At that point set `ADMIN_ERRORS_TEST_PG_URL` and run the suite with `DJANGO_DB=postgres`.
  Docker is running on this machine and Postgres.app is installed (psql on PATH, port 5432) — either the
  demo `docker-compose.yml` (postgres:16) or the local server is acceptable; prefer the compose service so
  the result is reproducible. Every phase's acceptance criteria that mention PostgreSQL are satisfied at
  that pass, not phase-by-phase — but any code written before it must still honour the Postgres rules in
  Section 16 (savepoints around racing creates, no `select_for_update`).

- **End-to-end / browser:** **On.** The surface is the demo project from Section 13
  (`demo/manage.py runserver`, admin at `http://127.0.0.1:8000/admin/`, superuser `admin`/`admin` created
  by `demo_seed`). The e2e layer is only meaningful from **Phase 4** onward (the admin UI); earlier phases
  have no user-facing surface and the e2e step should no-op there. The e2e up/down commands are **not
  pinned here** because `demo/` does not exist yet — the architect step must define them and write them
  into the run state. Requirements for them:
  - up must be idempotent (starting an already-running server must succeed, not fail on a taken port),
    must run `migrate` and `demo_seed` first, and must either return once the server is up or be paired
    with the ready URL `http://127.0.0.1:8000/admin/login/`;
  - down must stop the server it started;
  - never point them at anything but the local demo project.
  The browser pass also produces the README screenshots the spec asks for in `docs/img/` (issue list and
  issue detail, light and dark), and performs the manual QA script at the end of Section 13.

- **Web access:** **On.** Spec Section 3 requires the supported Django version list to be verified on
  djangoproject.com at implementation time; the test/CI matrix depends on it. Use web access for that and
  for checking current versions of dev tooling — not for anything else.

- **GitHub:** commits and pushes as `korkholeh` to `korkholeh/django-admin-errors` (the repo exists and is
  empty; this run makes its first commit). Push per phase. **No pull requests** and **no merging into the
  default branch** — the work stays on the run's `autodev/…` branch for review in the morning.

## Defaults the developer accepted

- `uv run pytest -q` over `make test`, because it removes a Makefile indirection from the loop the
  orchestrator drives, and uv already manages the toolchain on this machine.
- SQLite per phase with a dedicated PostgreSQL pass, because a per-phase PG run costs time on every phase
  and risks stalling the night on a container/connection problem, while the bugs it catches (savepoints,
  `IntegrityError` races) are concentrated in `storage.py` and can be caught in one pass.
- Web access on, because the Django support matrix is an explicit "verify at implementation time" in the spec.

## Facts resolved before the run (do not re-litigate)

- **PyPI name is free.** `https://pypi.org/pypi/django-admin-errors/json` returns 404, so the package name
  `django-admin-errors` is available. The Section 3 fallback to `django-admin-errorlog` is **not** needed.
  Import name stays `admin_errors`.
- **ruff 0.16.7** is installed via `uv tool install ruff` and on PATH.
- The GitHub repository `korkholeh/django-admin-errors` exists, is public, and has no commits and no
  default branch yet.
- Docker is running; Postgres.app is installed with `psql` on PATH.

## Left open on purpose

- **e2e up/down commands** — the architect step defines them once `demo/` exists (see above for the contract).
- **Ordering of the PostgreSQL pass** — the roadmap decides whether it is its own phase after Phase 3 or is
  folded into Phase 6; either is acceptable as long as the suite is green on PostgreSQL before the run ends
  and the result is recorded in `PROGRESS.md`.
- **Release to PyPI** — out of scope for this run. Build and `twine check` per Section 18, but do not upload.
- Everything the spec already decides (Section 5 defaults, Section 16 gotchas) stands; Section 0 says take
  the value marked **Default** and do not ask.
