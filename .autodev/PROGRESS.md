# Autodev progress — django-admin-errors

- **Status:** running
- **Current:** phase 1/10 · step `plan`
- **Spec:** `docs/spec.md` · **Branch:** `autodev/spec-20260915-1213`
- **Stack:** Python 3.10-3.13, Django 4.2/5.2/6.0/6.1, SQLite + PostgreSQL 13+, zero runtime deps beyond Django, hatchling src-layout wheel, pytest + pytest-django, ruff, tox, pytest-playwright for e2e · **Profile:** `django-htmx`
- **Test command:** `uv run pytest -q` · **E2E:** `uv run --extra e2e pytest e2e -q`
- **Usage:** 5h ? (reset 15.09 12:40) · 7d ?
- **Totals:** 2 sessions · 0.3 h agent time · ≈$5.10 API-equivalent
- **Clock:** 0.3 h since the run was created · 0.3 h working · 0.0 h paused on the usage limit · 0.0 h not running
- **Updated:** 2026-09-15 12:32:53

## Phases

| # | Phase | User-facing | Status | Commit | Warnings |
|---|---|---|---|---|---|
| 1 | Scaffold and toolchain | no | ⏳ pending |  |  |
| 2 | Settings proxy, models and fingerprint | no | ⏳ pending |  |  |
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

---
Docs: `ARCHITECTURE.md` · `RISKS.md` · `ROADMAP.md` · `DECISIONS.md` · `phases/*/PLAN.md` · `phases/*/REVIEW-r*.md` · `HANDOFF.md` (written at the end) · project docs in `docs/dev/` and `docs/user/` · raw session logs in `logs/`. Stop gracefully: `touch .autodev/STOP`.
