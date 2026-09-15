# Autodev progress — django-admin-errors

- **Status:** running
- **Current:** phase 2/10 · step `commit`
- **Spec:** `docs/spec.md` · **Branch:** `autodev/spec-20260915-1213`
- **Stack:** Python 3.10-3.13, Django 4.2/5.2/6.0/6.1, SQLite + PostgreSQL 13+, zero runtime deps beyond Django, hatchling src-layout wheel, pytest + pytest-django, ruff, tox, pytest-playwright for e2e · **Profile:** `django-htmx`
- **Test command:** `uv run pytest -q` · **E2E:** `uv run --extra e2e pytest e2e -q`
- **Usage:** 5h ? (reset 15.09 17:40) · 7d ?
- **Totals:** 18 sessions · 1.2 h agent time · ≈$21.99 API-equivalent
- **Clock:** 1.2 h since the run was created · 1.2 h working · 0.0 h paused on the usage limit · 0.0 h not running
- **Updated:** 2026-09-15 13:25:40

## Phases

| # | Phase | User-facing | Status | Commit | Warnings |
|---|---|---|---|---|---|
| 1 | Scaffold and toolchain | no | ✅ done | cc03ee9 |  |
| 2 | Settings proxy, models and fingerprint | no | 🔨 in_progress |  |  |
| 3 | Capture pipeline and storage (sync transport) | no | ⏳ pending |  |  |
| 4 | Background writer, sampling and signals | no | ⏳ pending |  |  |
| 5 | Retention, commands, dedicated alias and Celery | no | ⏳ pending |  |  |
| 6 | PostgreSQL pass | no | ⏳ pending |  |  |
| 7 | Demo project | yes | ⏳ pending |  |  |
| 8 | Admin UI | yes | ⏳ pending |  |  |
| 9 | Notifications, status transitions and i18n | yes | ⏳ pending |  |  |
| 10 | Documentation, release readiness and final verification | yes | ⏳ pending |  |  |

## Timeline

- `2026-09-15 12:26:11` **architect** — done (12m, $3.3): django-admin-errors is a reusable Django app that records unhandled exceptions and error-level log records into the host project's own database and shows them Sentry-style in the Django admin. The shape is a pipeline split at a bounded in-process queue: the capturing thread does guards, fingerprint…
- `2026-09-15 12:26:27` **push** — done: korkholeh/django-admin-errors@autodev/spec-20260915-1213 as korkholeh
- `2026-09-15 12:32:53` **roadmap** — done (6m, $1.8): `django-admin-errors` (import name `admin_errors`) is a zero-dependency reusable Django app that records unhandled exceptions and error-level log records into the host project's own database and shows them Sentry-style inside the Django admin — grouped by fingerprint, deduplicated, with traceback, …
- `2026-09-15 12:32:55` **push** — done: korkholeh/django-admin-errors@autodev/spec-20260915-1213 as korkholeh
- `2026-09-15 12:39:26` **p01-plan** — done (6m, $1.44): Wrote .autodev/phases/01-scaffold/PLAN.md for the scaffold phase: context (docs-only repo, pure creation), design of hatchling/src-layout packaging, the minimal-host tests/settings.py with a testable postgres DSN parser, the side-effect-free package plus empty AppConfig, the make-3.81-safe Makefile…
- `2026-09-15 12:43:41` **p01-implement** — done (4m, $1.13): Built full Phase 1 scaffold: pyproject.toml (hatchling, src layout, zero-dep + dev/e2e/celery/postgres extras), tox.ini (11 sqlite cells + lint/postgres/celery/package), Makefile (e2e-up/down, test, lint, build, demo placeholders), .github/workflows/ci.yml (5 jobs), .pre-commit-config.yaml, README/…
- `2026-09-15 12:43:41` **p01-tests** — pass: exit 0: 7 passed in 0.03s
- `2026-09-15 12:49:04` **p01-review1** — changes_requested (5m, $1.73): Scaffold is well-shaped and the day-to-day loop genuinely works: `uv run pytest -q` (7 passed), the four-segment lint, the e2e up/run/down cycle with port 8000 left free, `python -m build` + `twine check` (wheel and sdist), `tox -e lint`, `tox -e celery` and a sqlite matrix cell all exit 0 when I r…
- `2026-09-15 12:51:47` **p01-review_fix1** — done (2m, $0.69): All 5 review findings fixed. Fixed: BLOCKER1 (dev tools moved pyproject.toml optional-dependencies→dependency-groups so `uv run tox`/`uv run pytest` work on clean checkout — verified with clean UV_PROJECT_ENVIRONMENT); BLOCKER2 (tox.ini postgres envs were shadowed by generic `{sqlite,postgres}` sec…
- `2026-09-15 12:51:47` **p01-tests** — pass: exit 0: 14 passed in 0.04s
- `2026-09-15 12:55:32` **p01-review2** — changes_requested (3m, $1.52): Round 2 is close: all five r1 findings are genuinely fixed and I re-verified each one by running it — clean-env `uv run pytest -q` (14 passed) and `uv run tox --version` now work via `[dependency-groups] dev`; `tox c -e py313-dj52-postgres` shows `DJANGO_DB=postgres` + `extras=postgres` with `ADMIN…
- `2026-09-15 12:57:46` **p01-review_fix2** — done (2m, $0.45): Fixed all r2 findings: - BLOCKER: ci.yml sqlite-matrix used dotted python versions ({python:"3.10"}) building tox env names tox rejects (py3.10-dj42-sqlite, exit 254). Fixed by giving each matrix row an explicit dotless toxenv field and running `uv run tox -e ${{ matrix.toxenv }}`. Rewrote test_ci_…
- `2026-09-15 12:57:46` **p01-tests** — pass: exit 0: 14 passed in 0.04s
- `2026-09-15 12:59:43` **p01-review_audit2** — approve (1m, $0.64): All three r2 findings are fixed in the product by this diff and each was re-verified by running it. BLOCKER (ci.yml built a dotted tox env name): the sqlite-matrix rows now carry explicit dotless `toxenv` values and the step runs `uv run tox -e ${{ matrix.toxenv }}`; `uv run tox c -e <all 11 names>…
- `2026-09-15 13:00:41` **p01-docs** — done (0m, $0.38): Verified CLAUDE.md, README.md, CHANGELOG.md against the actual phase-1 diff (pyproject.toml extras/dependency-groups, Makefile targets, tox.ini env layout, ci.yml jobs, LICENSE). All already accurate — no edits needed. docs/dev/ has only adr/ (0001-0008, none touched by this phase); no testing.md/d…
- `2026-09-15 13:00:41` **p01-docs** — no changes: the documentation was already true for this phase
- `2026-09-15 13:00:41` **commit** — command files: this commit changes what the project's own commands run: Makefile, pyproject.toml, tox.ini
- `2026-09-15 13:00:41` **p01-commit** — done: cc03ee9
- `2026-09-15 13:00:44` **push** — done: korkholeh/django-admin-errors@autodev/spec-20260915-1213 as korkholeh
- `2026-09-15 13:04:53` **p02-plan** — done (4m, $1.43): Wrote .autodev/phases/02-settings-models-fingerprint/PLAN.md with Context, Design, 10 ordered tasks, a Verification table mapping every acceptance criterion to a named test, Risks (#7, #19, #12, #5, #2, #16) and Out of scope. Design pins the settings proxy (cached, cleared on setting_changed), W001…
- `2026-09-15 13:09:21` **p02-implement** — done (4m, $1.54): Built Phase 2 in full: src/admin_errors/conf.py (Settings proxy, all 38 spec keys, setting_changed cache reset), checks.py (W001 unknown-key warning, E002 SQLite<3.9 error, registered in apps.py ready()), models.py (Issue/Event/IssueDailyCount with explicit index/constraint names, view_issue_contex…
- `2026-09-15 13:09:22` **p02-tests** — pass: exit 0: 57 passed in 0.15s
- `2026-09-15 13:13:40` **p02-review1** — changes_requested (4m, $1.57): Phase 2 lands cleanly against its goal: the settings proxy, the three models, the single initial migration and the frozen fingerprint are all present, all gate commands pass on a fresh run (57 tests, ruff, django check, makemigrations --check all green), every PLAN task is [x] with no environment-b…
- `2026-09-15 13:16:17` **p02-review_fix1** — done (2m, $0.82): Fixed both majors: checks tests now go through run_checks() registry (proves ready() registration is load-bearing) instead of calling check functions directly; Issue.resolved_by got related_name="+" (migration regenerated) to stop the User.issue_set clash risk. Fixed all minors: dropped tautologica…
- `2026-09-15 13:16:17` **p02-tests** — pass: exit 0: 59 passed in 0.16s
- `2026-09-15 13:21:18` **p02-review2** — changes_requested (5m, $2.1): Phase 2 hits its goal: the settings proxy, the three models, exactly one initial migration and the frozen fingerprint are all present, and every acceptance criterion maps to a real test that can fail. I re-verified independently: 59 tests pass, `django check` and `makemigrations --check --dry-run` …
- `2026-09-15 13:22:19` **p02-review_fix2** — done (1m, $0.44): Fixed both majors and both minors, plus trivial nit from REVIEW-r2. Major1: added `extend-exclude = [".autodev"]` to `[tool.ruff]` in pyproject.toml so ruff format --check no longer breaks on Markdown Python fences in review docs. Major2: `normalize_message` now skips leading blank lines before tak…
- `2026-09-15 13:22:19` **p02-tests** — pass: exit 0: 63 passed in 0.14s
- `2026-09-15 13:24:11` **p02-review_audit2** — approve (1m, $0.63): Both majors, both minors and the nit from REVIEW-r2 are genuinely fixed in the product by this diff, each with a test that fails without the fix. Verified independently: `ruff check .` and `ruff format --check .` both exit 0 (the previously red gate is green — 32 files formatted, and no .py exists …
- `2026-09-15 13:25:40` **p02-docs** — done (1m, $0.38): Checked docs against phase-2 diff (conf.py, checks.py, models.py, fingerprint.py, migration). CLAUDE.md, README.md and CHANGELOG.md already true (no command changes this phase; fingerprint/migration pitfalls already noted). Only fix: docs/dev/adr/0003-fingerprint-is-a-frozen-public-contract.md desc…

---
Docs: `ARCHITECTURE.md` · `RISKS.md` · `ROADMAP.md` · `DECISIONS.md` · `phases/*/PLAN.md` · `phases/*/REVIEW-r*.md` · `HANDOFF.md` (written at the end) · project docs in `docs/dev/` and `docs/user/` · raw session logs in `logs/`. Stop gracefully: `touch .autodev/STOP`.
