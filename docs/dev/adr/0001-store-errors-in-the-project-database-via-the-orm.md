# 0001. Captured errors are stored in the host project's own database through the Django ORM

- **Status:** accepted
- **Date:** 2026-09-15

## Context

The product exists for environments where Sentry is overkill or forbidden: data residency rules,
air-gapped networks, small internal tools (spec §1). The operator is a single developer who already
runs a Django project with a database, and who already lives in the Django admin.

Forces:

- Zero runtime dependencies beyond Django is a stated pillar (spec §1, §3). Anything that needs a
  broker, a search engine or a time-series store violates it before the first line of code.
- The install story must be `pip install` → `INSTALLED_APPS` → `migrate`. Nothing else.
- Both SQLite and PostgreSQL ≥ 13 are first-class (spec §3), which rules out anything
  PostgreSQL-specific (`ON CONFLICT`-only upserts, `jsonb` operators, `LISTEN/NOTIFY`, array columns).
- Writing into the host's *production* database means our failures and our growth are the host's
  problem. That constraint is what ADR 0002 and ADR 0006 exist to contain.

## Decision

All state lives in three Django models — `Issue`, `Event`, `IssueDailyCount` — in the host project's
own database, accessed exclusively through the Django ORM. No external store, no cache-as-storage, no
files on disk.

The database alias is configurable via `ADMIN_ERRORS["DATABASE"]` (default `"default"`). A shipped
`AdminErrorsRouter` plus a `DATABASES["errors"]` entry lets a host move all error traffic to a separate
alias — a separate SQLite file next to a PostgreSQL main DB, or a separate database on the same server.
System check `E001` fails loudly when the configured alias does not exist.

All SQL is portable ORM SQL. Specifically: no `select_for_update` anywhere (SQLite ignores it and the
upsert pattern does not need it), no `ON CONFLICT`, no JSON containment queries — `JSONField` is used as
an opaque document column and is only ever read whole (see ADR 0004).

## Alternatives considered

- **Run self-hosted Sentry / GlitchTip.** — rejected: it is the thing the product exists to avoid. It
  needs PostgreSQL, Redis, ClickHouse-or-equivalent and several services, which is more infrastructure
  than the applications this targets.
- **A separate storage engine bundled with the app (SQLite file of our own, LMDB, append-only log).** —
  rejected: introduces a writable filesystem path, file permissions, rotation and corruption handling,
  and takes the data out of the host's existing backup story. The dedicated-alias option delivers most
  of the isolation benefit with none of that.
- **Cache / Redis as the store.** — rejected: not durable, not queryable for the list view's filters and
  aggregates, and a hard new dependency on the exact component most likely to be broken when errors spike.
- **PostgreSQL-only, using `jsonb` and `ON CONFLICT`.** — rejected: the small internal tools this targets
  are overwhelmingly on SQLite, and the spec makes SQLite first-class.

## Consequences

**Buys.** A one-line install. Error data is inside the host's existing backups, migrations, access
control and network boundary — which is exactly the compliance argument. Filters, search, aggregation
and the admin come free from the ORM. Upgrades are just `migrate`.

**Costs.** Every error write is a write to the host's production database: added connections, added
write-lock contention (acute on SQLite), and added disk growth. Portable-ORM-only means the upsert paths
are select-then-update-then-savepointed-insert rather than a single `ON CONFLICT` statement, costing an
extra query per aggregate.

**Becomes harder.** Cross-project aggregation is impossible without a second copy. Very high error rates
(≫ 10k/s sustained) are out of reach — the bound is the host's DB, not our code.

**Revisit when.** A host reports sustained write-lock contention that the dedicated alias does not fix,
or a credible need appears for one error store shared across several Django projects.
