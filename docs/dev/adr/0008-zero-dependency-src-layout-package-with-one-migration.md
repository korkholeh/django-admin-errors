# 0008. Ship as a zero-runtime-dependency `src/`-layout package with a narrow public API and exactly one initial migration

- **Status:** accepted
- **Date:** 2026-09-15

## Context

This is a **library**, not an application. Its users are other people's Django projects, and the
delivery channel is a PyPI wheel that gets installed into an environment we do not control. Three
consequences follow, and they are what this decision has to serve:

1. **Every dependency is a constraint imposed on the host.** A library that pulls in `requests` or a
   charting package can conflict with what the host already pins. Spec §1 and §3 make "Django only" a
   pillar, and the target deployments (air-gapped, regulated) have review processes where each added
   package has a real cost.
2. **Every importable name is a promise.** Anything a host can import, some host will import, and
   removing it later is a breaking change.
3. **Every migration runs in someone else's production database.** Migrations are the most dangerous
   thing a Django library ships.

Supporting constraints: Python 3.10–3.13 across Django 4.2/5.2/6.0/6.1, the package must work in a
minimal host (`django.contrib.{admin,auth,contenttypes,sessions,messages}` only), and it must carry
non-Python files — templates, static assets and locale catalogues — that are easy to omit from a wheel
by accident.

## Decision

**Layout.** `src/admin_errors/`, built with hatchling from a single `pyproject.toml`. The `src/` layout is
the point: tests import the *installed* package, so a missing `package_data` entry or a
`MANIFEST.in` mistake fails in CI instead of in a user's environment. Distribution name
`django-admin-errors` (verified free on PyPI 2026-09-15); import name `admin_errors`; app label
`admin_errors`; verbose name **Errors**.

**Dependencies.** `dependencies = ["Django>=4.2"]` and nothing else. Optional extras, none of them
required at runtime: `dev` (pytest, pytest-django, ruff, coverage), `e2e` (pytest-playwright), `celery`,
`postgres` (`psycopg[binary]`). Every optional integration is behind a guarded import —
`integrations/celery.py` and `tasks.py` simply do not register anything when Celery is absent, and that
is a normal state, not an error.

**Public API — the complete list.** Everything else is internal and may change within 0.x:

- `admin_errors.api.capture_exception(exc=None, *, request=None, extra=None, fingerprint=None, level="error") -> str | None`
- `admin_errors.api.capture_message(message, *, level="error", request=None, extra=None, fingerprint=None) -> str | None`
- `admin_errors.api.flush(timeout: float | None = 2.0) -> None`
- `admin_errors.signals.{issue_created, issue_regressed, issue_status_changed}`
- `admin_errors.admin.register(site: AdminSite) -> None`
- `admin_errors.handlers.AdminErrorsHandler`, `admin_errors.middleware.RequestContextMiddleware`,
  `admin_errors.routers.AdminErrorsRouter` — referenced by dotted path from host settings, so their
  import paths are part of the contract
- `admin_errors.models.{Issue, Event, IssueDailyCount}`
- `admin_errors.__version__`
- the `ADMIN_ERRORS` settings dict and the `Issue` database schema

`src/admin_errors/__init__.py` contains `__version__` **only** and stays free of import side effects, so
that importing the package never triggers app loading, settings access or thread creation. All wiring
happens in `AppConfig.ready()`, which must not query the database, start a thread, or touch the
filesystem.

**Type hints** on all public functions; mypy is available locally but is not a CI gate.

**Migrations.** Exactly **one** initial migration ships in 0.1.0; any migrations created during
development are squashed before release. After 0.1.0, migrations are additive only. The
`view_issue_context` permission is declared in `Issue.Meta.permissions` and therefore created by that
initial migration (ADR 0007).

**Release verification.** A `package` CI job runs `python -m build`, then `twine check`, then installs
the resulting wheel into a clean virtualenv and runs `django-admin check` and `migrate` against a minimal
project on SQLite. That job is what proves the wheel actually contains the templates, static files and
locale catalogues.

## Alternatives considered

- **Flat layout (`admin_errors/` at the repo root).** — rejected: tests would import the source tree
  directly and a broken wheel manifest would ship green. The `src/` layout is the only cheap way to test
  what users get.
- **setuptools with `setup.py`/`MANIFEST.in`.** — rejected: hatchling declares package data in
  `pyproject.toml` with no second file to drift out of sync, and is the current default for new packages.
  Poetry was rejected for a library because its dependency resolver's pins are the host's problem.
- **Vendor a small dependency or two (a charting helper, `typing_extensions`).** — rejected: ADR 0005
  removes the need for the former, and Python 3.10 is a high enough floor for the latter. Vendoring also
  means owning security updates for someone else's code.
- **Make Celery a real dependency to simplify the integration.** — rejected: most target hosts have no
  Celery at all, and a guarded import costs a try/except.
- **`Django>=4.2,<7`, i.e. an upper pin.** — rejected: upper-pinning Django in a library is actively
  harmful — it blocks hosts from upgrading Django on the library's release schedule. The CI matrix is the
  honest statement of what is tested; a pin would be a false statement of what works.
- **Ship several migrations as development proceeds.** — rejected: 0.1.0 has no installed base, so there
  is nothing to preserve, and one clean migration is much easier for a host to review before running it
  against production.
- **Re-export everything from `admin_errors/__init__.py` for convenience.** — rejected: import side
  effects at package-import time are a known source of `AppRegistryNotReady`, and a wide `__init__`
  freezes far more of the internals as a public promise than intended.

## Consequences

**Buys.** Installs cleanly into any Django project without dependency negotiation. Passes a security
review on the strength of its dependency tree alone. A CI job that proves the artefact, not just the
source tree. A small, deliberately chosen public surface that leaves internals free to change.

**Costs.** Things other libraries get for free must be written here: the SVG charts, the settings proxy,
the LRU buckets, the token buckets. The support matrix is 12 Python×Django combinations plus four
specialised jobs, which is a real CI bill and a real maintenance cost. Guarded imports need their own
tests for both the present and absent case.

**Becomes harder.** Adding any dependency later is a breaking change to the value proposition, not just
a line in `pyproject.toml`. Changing the fingerprint (ADR 0003) or the `Issue` schema is a major-version
event because both are part of this public contract.

**Revisit when.** A supported Django version demands something the standard library cannot provide, or
Python 3.10 goes end-of-life and the floor can rise.
