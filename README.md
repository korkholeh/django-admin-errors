# django-admin-errors

Sentry-style error tracking, built into the Django admin. Unhandled exceptions and error-level log
records are captured into your own project's database — grouped into issues by fingerprint,
deduplicated, with traceback, request context, counts and 14/30-day trends. No external service, no
extra infrastructure: just your database and Django's own admin.

Use it when Sentry (or an equivalent SaaS) is overkill, not allowed by policy, or not worth the
operational cost for a small project. It is not a replacement for Sentry at scale: there is no
alerting pipeline, no release tracking, no cross-project search — see *Non-goals* below.

## Status

Pre-release (`0.1.0.dev0`). The scaffold, models, capture pipeline and admin UI are being built out
phase by phase; see `CHANGELOG.md` for what has actually landed.

## Compatibility

| Python | Django |
|---|---|
| 3.10 | 4.2, 5.2 |
| 3.11 | 4.2, 5.2 |
| 3.12 | 4.2, 5.2, 6.0, 6.1 |
| 3.13 | 5.2, 6.0, 6.1 |

Django 4.2 is upstream end-of-life but is kept as a CI-tested, best-effort target because the project
targets it as a floor; Django's own security support for 4.2 is the host project's responsibility, not
this package's. Databases: SQLite ≥ 3.9 (JSON1) and PostgreSQL ≥ 13. Zero runtime dependencies beyond
Django itself.

## Install

```sh
pip install django-admin-errors
```

Installation and settings instructions land here as each phase ships (see `docs/spec.md` for the full
design). For now, see `CHANGELOG.md` under *Unreleased* for progress.

## Development

All commands run from the repository root, non-interactively, with no virtualenv activated:

| What | Command |
|---|---|
| install | `uv sync --all-extras` |
| test | `uv run pytest -q` |
| test on PostgreSQL | `DJANGO_DB=postgres ADMIN_ERRORS_TEST_PG_URL=postgres://postgres:postgres@localhost:5432/admin_errors_test uv run pytest -q` |
| lint | `uv run ruff check . && uv run ruff format --check . && uv run python -m django check --settings=tests.settings && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings` |
| full matrix | `uv run tox` |
| build | `uv run python -m build && uv run twine check dist/*` |
| e2e | `make e2e-up && uv run --extra e2e pytest e2e -q && make e2e-down` |
| demo (manual) | `make demo` (SQLite) · `make demo-pg` (Postgres via docker compose) |

See `docs/spec.md` for the full implementation specification and `docs/dev/adr/` for the accepted
design decisions.

## License

MIT, see `LICENSE`.
