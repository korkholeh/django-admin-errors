# Review — phase 1 round 2

**Verdict:** changes_requested

Round 2 is close: all five r1 findings are genuinely fixed and I re-verified each one by running it — clean-env `uv run pytest -q` (14 passed) and `uv run tox --version` now work via `[dependency-groups] dev`; `tox c -e py313-dj52-postgres` shows `DJANGO_DB=postgres` + `extras=postgres` with `ADMIN_ERRORS_TEST_PG_URL` passed through; `tox -e package` runs end to end (build → twine → wheel into a fresh venv → `django check` → `migrate`), exit 0; `database_from_url` unquotes username and database name with a test covering it; `tests/test_toolchain.py` exists. Every acceptance command exits 0 locally: the four-segment lint, `make e2e-up`/`pytest e2e`/`make e2e-down` (port 8000 free afterwards), build producing wheel + sdist with `twine check` PASSED, `tox -e lint`, `tox -e celery`, `tox -e py310-dj42-sqlite`, the version print with no settings, and coverage at 100% against `--cov-fail-under=90`. All PLAN tasks are checked, none `[~]`, and the deviations are logged in DECISIONS.md. One blocker remains and it is a false green of the same family as r1's: the CI sqlite matrix feeds dotted Python versions into the tox env name (`py3.10-dj42-sqlite`), which tox rejects with exit 254, so all 11 matrix jobs — the only enforcement of the support matrix and of the 90% coverage floor — cannot run; the new ci.yml test strips dots before comparing, so it structurally cannot catch it. Two minors: the sdist ships the toolchain tests without the ci.yml they read, and the package URLs name the wrong GitHub account.

## [BLOCKER] sqlite-matrix builds a tox env name that does not exist — all 11 cells fail before running a test
`.github/workflows/ci.yml`

The matrix rows carry dotted Python versions (`- { python: "3.10", django: "42" }`) and the step runs `uv run tox -e py${{ matrix.python }}-dj${{ matrix.django }}-sqlite`, which expands to `py3.10-dj42-sqlite`. tox.ini declares `py310-dj42-sqlite`. tox rejects the name outright:

    $ uv run tox -e py3.10-dj42-sqlite -- -q
    ROOT: HandledError| provided environments not found in configuration file:
    py3.10-dj42-sqlite - did you mean py310-dj42-sqlite?
    EXIT=254

Every one of the 11 sqlite-matrix jobs — the only jobs that run the suite across the support matrix and the only place `--cov-fail-under=90` is enforced — fails on the first push. The correct-name form works (`uv run tox -e py310-dj42-sqlite` → 14 passed). This is the same class of defect as r1's blockers: the matrix table *looks* right, so criterion 7 passes by reading.

Compounding it, the new regression test cannot fail on this: `tests/test_toolchain.py::test_ci_matrix_lists_exactly_the_profile_sqlite_cells` normalizes with `python.replace(".", "")` before comparing, so `"3.10"` and `"310"` are indistinguishable to it. The one criterion that needed executable proof still has none for the part that matters — whether the env name the workflow constructs actually resolves.

**Fix:** Give each matrix row the tox env name explicitly, e.g. `- { toxenv: "py310-dj42-sqlite" }` … and run `uv run tox -e ${{ matrix.toxenv }} -- -q --cov=admin_errors --cov-report=term-missing --cov-fail-under=90`; or keep `python`/`django` but make the value dotless (`python: "310"`). Then change the test to assert the exact env-name strings the workflow will pass (no dot-stripping) and cross-check them against `tox.ini`'s expanded `env_list` (e.g. parse `uv run tox l` output or expand the brace factors from the ini), so a name mismatch fails the suite. Re-verify with `uv run tox -e <the name ci.yml builds>`.

## [MINOR] sdist ships tests/test_toolchain.py but not .github/workflows/ci.yml, so `pytest` from an unpacked sdist errors
`pyproject.toml`

`[tool.hatch.build.targets.sdist] include = ["src/", "tests/", "tox.ini", …]` — verified with `tar tzf dist/django_admin_errors-0.1.0.dev0.tar.gz`: `tests/test_toolchain.py` is present, `.github/workflows/ci.yml` is not. `test_ci_matrix_lists_exactly_the_profile_sqlite_cells` and `test_ci_defines_the_five_jobs` both do `(repo_root / ".github" / "workflows" / "ci.yml").read_text()`, so anyone running the suite from the sdist (distro packagers, the Phase 10 clean-venv proof) gets a FileNotFoundError, not a pass.

**Fix:** Either add `.github/workflows/ci.yml` to the sdist `include` list, or make the two ci.yml tests skip when the file is absent (`pytest.skip("ci.yml not shipped in sdist")` guarded on `path.exists()`) — the tox.ini half of the file still runs in that case.

## [MINOR] [project.urls] points at github.com/okorkh/… — wrong account, shipped in the wheel metadata
`pyproject.toml`

`Homepage`/`Changelog` use `https://github.com/okorkh/django-admin-errors`. The repository has no remote configured (`git remote -v` prints nothing, exit 0), and the only recorded push in `.autodev/PROGRESS.md` is `korkholeh/django-admin-errors@autodev/spec-20260915-1213 as korkholeh`. As written, both PyPI links 404. Cheap to fix now, embarrassing at 0.1.0.

**Fix:** Change both URLs to `https://github.com/korkholeh/django-admin-errors` (and the `Changelog` blob path accordingly), or drop `[project.urls]` until the canonical repository URL is known.
