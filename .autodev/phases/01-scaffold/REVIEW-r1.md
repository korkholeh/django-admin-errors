# Review — phase 1 round 1

**Verdict:** changes_requested

Scaffold is well-shaped and the day-to-day loop genuinely works: `uv run pytest -q` (7 passed), the four-segment lint, the e2e up/run/down cycle with port 8000 left free, `python -m build` + `twine check` (wheel and sdist), `tox -e lint`, `tox -e celery` and a sqlite matrix cell all exit 0 when I ran them, ci.yml's 11 cells match PROFILE exactly, all PLAN tasks are checked with none marked `[~]`, and the deviations are logged in DECISIONS.md. But three toolchain defects survive, two of them false-greens: (1) every CI job runs `uv run tox …` while tox sits in the `dev` extra, which `uv run` does not install — proven to fail with `error: Failed to spawn: tox` in a clean project environment; (2) both `*-postgres` tox envs are shadowed by the generic dj52/dj61 sections, so `tox c` shows `extras = dev` and no `DJANGO_DB` — the CI postgres job tests SQLite and passes; (3) `tox -e package` fails because tox does not expand `dist/*.whl`, leaving risk #17 (wheel completeness) with no working check at all, since the matrix installs the package editable. The common root is that nothing automated verifies the tox/CI wiring, which is also criterion 7's missing proof.

## [BLOCKER] Every CI job fails: `uv run tox` cannot find tox on a clean checkout
`.github/workflows/ci.yml`

All five jobs run `uv run tox …` with only `actions/checkout` + `astral-sh/setup-uv` before them. `tox` (and `pytest`, `ruff`, `build`, `twine`) live in the `dev` optional-dependency extra, and `uv run` syncs only the project's default dependencies — extras are not installed. Proven on a clean project environment:

    $ cd repo && D=$(mktemp -d) && UV_PROJECT_ENVIRONMENT="$D/venv" uv run tox --version
    Using CPython 3.11.15
    Creating virtual environment at: /var/folders/.../venv
    Installed 4 packages in 104ms
    error: Failed to spawn: `tox`
      Caused by: No such file or directory (os error 2)

Same result for `uv run pytest -q`. Locally the commands only pass because `uv sync --all-extras` was run first, so the .venv already holds the dev tools. Consequence: the lint, sqlite-matrix, postgres, celery and package jobs are all red on the first push — criterion 7's workflow defines the jobs but none of them can execute, and the acceptance criterion "`uv run pytest -q` exits 0 on a clean checkout" only holds after the separate install step.

**Fix:** Move the dev tooling from `[project.optional-dependencies] dev` to `[dependency-groups] dev` in pyproject.toml — uv installs the default group automatically, so `uv run pytest -q` and `uv run tox` work on a clean checkout with no install step (tox's `extras = dev` then becomes `dependency_groups = dev`). Minimal alternative: add a `- run: uv sync --all-extras` step to every job, or use `uv run --extra dev tox …`.

## [BLOCKER] PostgreSQL tox envs resolve without `DJANGO_DB` or psycopg — the CI postgres job silently tests SQLite
`tox.ini`

`[testenv:py313-dj{52,61}-postgres]` (extras `dev,postgres`, `set_env DJANGO_DB=postgres`, `pass_env ADMIN_ERRORS_TEST_PG_URL`) is never applied: the earlier generic sections `[testenv:py{310,311,312,313}-dj52-{sqlite,postgres}]` and `[testenv:py{312,313}-dj61-{sqlite,postgres}]` already match those env names and tox 4 uses the first matching section. Verified:

    $ uv run tox c -e py313-dj52-postgres
    set_env =
      DJANGO_SETTINGS_MODULE=tests.settings
      PYTHONHASHSEED=... PYTHONIOENCODING=utf-8      # no DJANGO_DB
    extras = dev                                      # no `postgres` → psycopg absent
    $ uv run tox c -e py313-dj61-postgres   # identical

With `DJANGO_DB` unset, tests/settings.py takes the SQLite branch, so the CI `postgres` job (which sets DJANGO_DB/ADMIN_ERRORS_TEST_PG_URL as job env, but tox does not pass unknown env vars through without `pass_env`) runs the suite twice on in-memory SQLite and reports green. The PROFILE support matrix's `postgres (py313 × dj52, dj61)` cells are therefore not covered, and risk #5 (PG-specific behaviour found late) stays fully open — the phase that is supposed to catch it is Phase 6, which will inherit a mis-wired env.

**Fix:** Give the postgres envs non-overlapping sections: restrict the generic ones to `-sqlite` (`[testenv:py{310,311,312,313}-dj52-sqlite]`, `[testenv:py{312,313}-dj61-sqlite]`) and declare `[testenv:py313-dj52-postgres]` / `[testenv:py313-dj61-postgres]` explicitly with their own `deps`, `extras = dev,postgres`, `set_env DJANGO_DB=postgres` and `pass_env ADMIN_ERRORS_TEST_PG_URL`. Re-verify with `uv run tox c -e py313-dj52-postgres` before calling it done.

## [MAJOR] `tox -e package` fails — `dist/*.whl` is not glob-expanded, so risk #17 has no working check
`tox.ini`

tox runs commands without a shell, so the glob in `uv pip install --python .tox/package-install/bin/python Django>=4.2 dist/*.whl` is passed literally:

    package: commands[4]> uv pip install --python .tox/package-install/bin/python 'Django>=4.2' 'dist/*.whl'
    error: The wheel filename "*.whl" is invalid: Must have a version
    package: FAIL code 1

(`twine check dist/*` survives only because twine globs internally.) The CI `package` job is red, and with it the only proof that the wheel is complete. The plan and `.autodev/DECISIONS.md` (`p01-implement/tox`) describe this env as working; it was never executed — the PLAN verification table lists `python -m build`/`twine check` but not `tox -e package`, and T11 is nevertheless checked off. Compounding it, `[testenv] package = editable` means the sqlite matrix imports the source tree, not a built wheel, so PROFILE's "tests import the installed package, so a missing package_data entry fails in CI" does not hold either: risk #17 currently has zero coverage, right before Phase 8 adds templates and static files.

**Fix:** Replace the glob with a resolvable requirement, e.g. `uv pip install --python .tox/package-install/bin/python --find-links dist django-admin-errors 'Django>=4.2'`, and run `uv run tox -e package` to confirm exit 0. Optionally set `package = wheel` in `[testenv]` so the matrix also imports a built wheel.

## [MAJOR] Nothing automated verifies the CI/tox wiring; acceptance criterion 7 rests on manual reading only
`.github/workflows/ci.yml`

Criterion 7 ("ci.yml defines the lint, sqlite-matrix, postgres, celery and package jobs, and the sqlite matrix lists exactly the Python×Django cells from PROFILE.md") is the one criterion with no executable proof. PLAN.md waives it explicitly: "`uv run python -c` reading ci.yml is not available (no yaml runtime dep); verified by reading the file". The cells do match today, but the three defects above — none of which a human reading catches — show the cost: two jobs are structurally incapable of running and one reports a false green. A stdlib-only test (configparser over tox.ini, a regex over the `matrix.include` block) would have pinned the job list and the cell list, and `tox c --json` output would have pinned the postgres env's extras/set_env.

**Fix:** Add `tests/test_toolchain.py`: parse `tox.ini` with `configparser`, assert `env_list` contains exactly the 11 sqlite cells of the PROFILE matrix plus lint/postgres/celery/package; scrape `matrix.include` from ci.yml with a regex and assert the same 11 pairs and that the five job names are present; assert each `*-postgres` env section is uniquely matching (or assert on `tox c -e … --json`). PyYAML in the dev extra would make the ci.yml half exact without touching runtime deps.

## [MINOR] `database_from_url` unquotes the password but not the username or database name
`tests/settings.py`

Line 20-21: `PASSWORD` goes through `unquote()`, `USER` (and `NAME`, taken from `parts.path`) does not. `urlsplit().username` is percent-encoded like the password, so a DSN such as `postgres://svc%40tenant:pw@h:5432/db` yields `USER = "svc%40tenant"` and fails to authenticate, with a confusing error. The test suite only covers an encoded password (`test_database_from_url_parses_postgres_dsn`), so the asymmetry is invisible.

**Fix:** Apply `unquote()` to the username and to the path-derived database name too, and extend the parser test with an encoded username, e.g. `postgres://u%40tenant:p%40ss@h:5433/db` → `USER == "u@tenant"`.
