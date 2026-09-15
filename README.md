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
| test on PostgreSQL (compose) | `make test-pg` — brings up `demo/docker-compose.yml`'s `postgres:16` service, runs the suite, always tears it down |
| bring up / down the PG container | `make pg-up` / `make pg-down` |
| lint | `uv run ruff check . && uv run ruff format --check . && uv run python -m django check --settings=tests.settings && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings` |
| full matrix | `uv run tox` |
| build | `uv run python -m build && uv run twine check dist/*` |
| e2e | `make e2e-up && uv run --extra e2e pytest e2e -q && make e2e-down` |
| demo (manual, blocks on `runserver`) | `make demo` (SQLite) · `make demo-pg` (Postgres via docker compose) |

See `docs/spec.md` for the full implementation specification and `docs/dev/adr/` for the accepted
design decisions.

## Try it

`demo/` is a small, throwaway Django project (never published in the package) that exercises every
capture behaviour by hand, including the *Errors* section of the admin (issue list with sparklines,
detail page with traceback/request/occurrences, Resolve/Ignore/Reopen). The technical 500 page and
the console-logged exceptions are also worth poking at on their own.

```sh
make demo       # SQLite: migrate, seed 40 issues over 30 days, runserver 127.0.0.1:8000
make demo-pg    # same, against the docker-compose postgres:16 service (make pg-up/pg-down)
```

Both block on `runserver`; stop them with Ctrl-C. Log in at `/admin/` as `admin` / `admin` (created,
or its password reset, by `demo_seed` on every run). Then, from `/` (the index lists every link with
a one-line description):

| URL | What it shows |
|---|---|
| `/boom/` | `ZeroDivisionError`, unhandled → 500 |
| `/boom/<int:n>/` | `ValueError` with `n` in the message — message normalization: any `n` groups into the same issue |
| `/keyerror/<slug>/` | `KeyError` on `slug` — same grouping, any key |
| `/nested/` | a `RuntimeError` raised `from` an inner `ValueError` — chained exception stored |
| `/logged/` | caught, `logger.exception(...)`, still captured, returns 200 |
| `/warning/` | `logger.warning(...)` with `extra` — captured because the demo's `CAPTURE_LEVEL` is `"WARNING"` |
| `/sensitive/` | POST form with a password/token — payload is scrubbed, nothing sensitive stored |
| `/storm/?n=1000` | `n` occurrences of the same error in a loop — one issue, `count` grows by up to `n` (a tight burst can overflow the writer's bounded queue and drop some), only a handful of events stored (`EVENT_SAMPLE_PER_HOUR`) |
| `/unique-storm/?n=200` | `n` distinct fingerprints — new-issue creation capped by `NEW_ISSUES_PER_MINUTE` |
| `/task/` | a Celery task failure (eager without a broker, since the demo runs no worker) |
| `/async-boom/` | an `async def` view raising |
| `/404/` | `Http404` — confirms 404s are never captured |

Manual QA script (spec section 13): `make demo`, hit `/boom/` three times → one issue, count 3; hit
`/boom/1/` then `/boom/2/` → a second issue (`ValueError`, distinct from `/boom/`'s
`ZeroDivisionError`), count 2 despite the different `n`; hit `/storm/?n=5000` → response under 1 s,
one issue, at most `EVENT_SAMPLE_PER_HOUR` stored events, and the occurrence count grows by up to
5000 (a tight burst can overflow the writer's bounded queue and drop some occurrences — that counter
is process-local, so it is only visible in the same process, not through a separate `manage.py`
invocation); run `uv run python demo/manage.py errors_cleanup --dry-run` → a report of what retention
would delete.
Resolving an issue in the admin and triggering it again shows the *Regressed* badge on its detail
page; the notification email for that event is a Phase 9 addition and not yet observable.

## License

MIT, see `LICENSE`.
