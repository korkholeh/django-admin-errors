# Phase 6 — PostgreSQL pass

**Goal:** prove the storage and retention layers on PostgreSQL while `storage.py`/`retention.py` are still
fresh, so no savepoint or aborted-transaction defect surfaces at the end of the run (RISKS #5).

## Context

What already exists (verified by reading the code, not assumed):

- `src/admin_errors/storage.py` — `store_batch()` -> `_store_one()` per fingerprint, one
  `transaction.atomic(using=alias)` per aggregate. Racing creates are **already** savepointed:
  `_create_issue()` wraps `Issue.objects.create()` in a nested `atomic()` and re-selects on
  `IntegrityError` (re-raising when the re-select finds nothing), and `_update_daily_counts()` uses
  update -> savepointed create -> update-on-`IntegrityError`. Counters use `F("count") + n` and
  `Greatest(F("last_seen"), last_ts)`. No `select_for_update` anywhere (`grep` clean today).
- `src/admin_errors/retention.py` — seven rules, all chunked through `_delete_in_chunks` /
  `_evict_status`; rule 3 deliberately avoids `Greatest` for backend-identical NULL semantics
  (DECISIONS p05-plan/retention); `_vacuum()` already returns `None` for `vendor != "sqlite"`.
- `src/admin_errors/models.py` — `Issue.fingerprint` unique, `IssueDailyCount` unique
  (`issue`, `date`), two `JSONField`s (`Issue.last_event`, `Event.payload`). One migration.
- `tests/settings.py` — `DJANGO_DB=postgres` switches `default` to the DSN in
  `ADMIN_ERRORS_TEST_PG_URL` (default `postgres://postgres:postgres@localhost:5432/admin_errors_test`);
  the second `errors` alias stays SQLite on purpose (p05-plan/tests), so the PG run needs exactly one
  PostgreSQL database and no extra grants.
- `tox.ini` has `py313-dj{52,61}-postgres` (extras `postgres`, `DJANGO_DB=postgres`,
  `pass_env = ADMIN_ERRORS_TEST_PG_URL`); `.github/workflows/ci.yml` already has a `postgres` job on
  `postgres:16` with the same DSN. `pyproject.toml` has the `postgres` extra (`psycopg[binary]>=3.1`,
  resolving to psycopg 3.3.5 locally).
- Existing PG-aware cases: `tests/test_commands.py` (`pg_total_relation_size`) and
  `tests/test_retention.py` (vacuum no-op on PG) each skip inline on `connection.vendor`; they are the
  "1-2 skipped" of the SQLite run.
- `demo/` does **not** exist yet (Phase 7). `Makefile: e2e-up` already exits 0 when `demo/manage.py`
  is missing, and `make test-pg` today is only `DJANGO_DB=postgres uv run pytest -q` — no server.

Environment, checked at plan time: `docker version` -> server `29.7.2`, `docker ps` -> no containers;
`uv run --extra postgres python -c "import psycopg"` -> `3.3.5`.

What this phase changes: adds `demo/docker-compose.yml`, real `pg-up` / `test-pg` / `pg-down` Makefile
targets, a `postgres_only` fixture, a new `tests/test_postgres.py`, one payload-sanitisation fix in
`context.py` (see Design), plus whatever the first full PG run turns up. Key files:
`demo/docker-compose.yml`, `Makefile`, `tests/conftest.py`, `tests/test_postgres.py`,
`tests/test_toolchain.py`, `src/admin_errors/context.py`, `CHANGELOG.md`.

## Design

### 1. The PostgreSQL target

`demo/docker-compose.yml`, one service:

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: admin_errors_test
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d admin_errors_test"]
      interval: 2s
      timeout: 3s
      retries: 30
    volumes: [admin_errors_pgdata:/var/lib/postgresql/data]
volumes:
  admin_errors_pgdata:
```

The DSN this exposes is exactly the one `tests/settings.py:DEFAULT_PG_URL`, `ci.yml` and PROFILE.md name.
`POSTGRES_USER=postgres` is a superuser, so Django can create `test_admin_errors_test` without extra
grants. A named volume (not `tmpfs`) because Phase 7's `make demo-pg` reuses this same service and wants
its data to survive a restart; `docker compose down` (no `-v`) keeps it, `down -v` wipes it.

No `version:` key (obsolete in Compose v2) and no `container_name` (lets a second checkout run its own).

### 2. Makefile targets

```
pg-up    docker compose -f demo/docker-compose.yml up -d --wait
pg-down  docker compose -f demo/docker-compose.yml down
test-pg  pg-up; DJANGO_DB=postgres ADMIN_ERRORS_TEST_PG_URL=... pytest -q; keep status; pg-down; exit status
```

`--wait` is the non-interactive form of "block until healthy" and needs the healthcheck above; there is
no polling loop to hand-write. `test-pg` always tears the container down and re-raises pytest's exit
code (`st=$$?; $(MAKE) pg-down; exit $$st`) — the run's "never leave a background process behind" rule
outranks the convenience of leaving a container up after a red run; `make pg-up` plus the raw pytest
line is the documented path for interactive debugging. GNU make 3.81: recipes stay single-command `sh`
lines joined with `;` / `&&`, no `.ONESHELL`.

`ADMIN_ERRORS_TEST_PG_URL ?= postgres://...` at the top of the Makefile, so an operator can point
`test-pg` at another server without editing it.

### 3. Test surface

- `tests/conftest.py` gains a `postgres_only` fixture (`pytest.skip` unless
  `connection.vendor == "postgresql"`). The two existing inline `connection.vendor` skips move onto it,
  so "why did this skip?" has one answer in the repo. A module-level `skipif` is not usable: the vendor
  is only knowable after Django is configured, which happens after collection imports the module.
- `tests/test_postgres.py` — new file, every case takes `postgres_only`, so the file is a block of skips
  on SQLite and real coverage under `DJANGO_DB=postgres`. It holds only what SQLite *cannot* exercise
  (taxonomy: concurrency, errors, persistence):
  1. **Savepoint isolation, issue create** — inside an outer `transaction.atomic()`, call
     `storage._create_issue()` for a fingerprint that already exists (the race's loser,
     deterministically). Assert it returns the existing row with `created=False` **and** that a query
     issued afterwards *in the same outer transaction* still works. Without the nested savepoint,
     PostgreSQL raises `InternalError: current transaction is aborted` on that follow-up query — the
     assertion SQLite structurally cannot make.
  2. **Savepoint isolation, daily count create** — same shape for `_update_daily_counts()`: force the
     update-returns-0-then-`IntegrityError` branch (pre-create the row, monkeypatch the update to return
     0), then assert the outer transaction survives and the count was added, not lost.
  3. **`store_batch` end to end after a swallowed `IntegrityError`** — two `store_batch()` calls for the
     same fingerprint inside one outer atomic block: one issue, summed count.
  4. **JSONB round trip** — a payload with nested dicts, non-ASCII and emoji reads back equal by key
     (JSONB does not preserve key order, so no string comparison).
  5. **NUL / lone-surrogate payload** — see section 4 below.
  6. **`vacuum=True` is a no-op on PG** — already exists in `test_retention.py`; it stays next to the
     other vacuum cases and only switches to the fixture.
  The two-thread race case (`test_two_threads_storing_one_new_fingerprint_create_one_issue`) and the
  three `assertNumQueries` budgets stay where they are: they are backend-agnostic by construction and
  the acceptance criteria want them *running under both* backends, not duplicated.
- `tests/test_storage.py` gains `test_last_seen_never_moves_backwards` (a late batch carrying an older
  `last_ts` must not rewind `last_seen`) — the `Greatest()` contract, unpinned today, and the one place
  where a "portable" rewrite of that expression would silently regress. Runs on both backends.
- `tests/test_toolchain.py` gains one stdlib-only case asserting the DSN contract is stated identically
  in `demo/docker-compose.yml`, `.github/workflows/ci.yml` and `tests/settings.py:DEFAULT_PG_URL`
  (image `postgres:16`, user/password/db, port 5432). Plain-text scraping, no PyYAML (that file's
  existing convention). This is what makes "the CI postgres job is verified against the same URL
  contract" a test rather than a claim.

### 4. The one fix predicted up front: NUL and surrogates in payloads

PostgreSQL rejects a NUL codepoint inside `jsonb` (`unsupported Unicode escape sequence`) **and** inside
`text` (psycopg: `A string literal cannot contain NUL (0x00) characters`); SQLite stores both happily.
Unpaired surrogates cannot even be UTF-8 encoded for the wire. Most payload strings go through
`context.safe_repr()`, whose `repr()` escapes both — but three paths carry raw text: `_exc_message()`
(`str(exc)`), the header/GET/POST strings in `build_request_block()` (values that are already `str` are
passed through verbatim), and `capture.py`'s `meta` (`title`, `culprit`, `exception_type`, which become
`Issue` CharFields). An exception whose message contains a NUL therefore aborts the write on PostgreSQL
only — exactly the class of defect this phase exists to find.

Fix: one helper in `context.py`, `sanitize_text(text) -> str`, replacing NUL with U+FFFD and stripping
lone surrogates, applied (a) recursively over the finished dict in `build_payload()` — one place, once,
over a payload already bounded by the `MAX_*` limits — and (b) to the four `meta` strings in
`capture.py`. Capture must never raise, so the walk sits inside the existing `try/except BaseException`
frames. It lives in `context.py` rather than `storage.py` because ARCHITECTURE.md's module table makes
`context.py` the owner of payload construction and `storage.py` a pure writer; sanitising at the writer
would also leave the `meta`/`Issue`-field path unprotected. Logged to DECISIONS.md.

If the first full PG run (T4) turns up nothing else, this is the phase's only product change — the
expected outcome, since the savepoint and `select_for_update` rules were honoured from Phase 3 on.

### 5. Error handling and architecture fit

No new failure modes: the phase adds no runtime code path, only a sanitiser on an existing one. The
savepoint discipline in ARCHITECTURE.md line 355 ("every create that can race is inside its own
savepoint ... no `select_for_update` anywhere") is what this phase *verifies*; nothing here changes it.
`demo/docker-compose.yml` arriving before the rest of `demo/` is the ROADMAP's own assumption and does
not make `demo/manage.py` exist, so `make e2e-up` keeps its no-op exit 0.

## Tasks

- [x] **T1** — `demo/docker-compose.yml`: the `postgres:16` service of section 1 (healthcheck, named
      volume, port 5432, `admin_errors_test`). Confirm `docker compose -f demo/docker-compose.yml config`
      parses and `up -d --wait` reaches healthy.
- [x] **T2** — `Makefile`: `ADMIN_ERRORS_TEST_PG_URL ?=` default plus `pg-up`, `pg-down` and the
      rewritten `test-pg` (section 2); add both new targets to `.PHONY`. Verify `make pg-up && make
      pg-down` leaves `docker ps` empty.
- [x] **T3** — `tests/conftest.py`: add the `postgres_only` fixture; move the two existing inline
      `connection.vendor` skips (`tests/test_commands.py`, `tests/test_retention.py`) onto it. The SQLite
      suite must stay green with the same skip count.
- [x] **T4** — **First full PostgreSQL run**, before writing any PG test: `make pg-up`, then
      `DJANGO_DB=postgres ADMIN_ERRORS_TEST_PG_URL=... uv run pytest -q`. Record every failure verbatim
      in `.autodev/DECISIONS.md` under `## p06-implement` before touching product code. This ordering is
      the point of the phase: the defects it finds drive T7, not a guess.
      Result: clean, 203 passed, 0 failures — see DECISIONS.md p06-implement/T4.
- [x] **T5** — `tests/test_postgres.py`: cases 1-4 of section 3 (savepoint isolation for the issue create
      and for the daily-count create, `store_batch` inside an outer atomic, JSONB round trip). All take
      `postgres_only`.
- [x] **T6** — `context.sanitize_text()` plus its use in `build_payload()` and `capture.py`'s `meta`
      (section 4). Tests: a backend-agnostic unit case in `tests/test_context.py` (NUL and a lone
      surrogate are replaced, ordinary text and emoji untouched, nested dict/list walked) and case 5 in
      `tests/test_postgres.py` (capturing an exception whose message carries a NUL stores an issue whose
      `title` and `last_event` read back on PostgreSQL).
- [x] **T7** — Fix anything else T4 surfaced in `storage.py` / `retention.py` / `models.py`, each with a
      regression test that fails on PostgreSQL before the fix. If T4 was clean, close the task with a
      DECISIONS line saying so and the exact run output.
      T4's own snapshot was clean; adding this phase's other new tests (T5/T6/T8/T9) lengthened the
      run enough to surface a real writer-thread PostgreSQL connection leak on shutdown. Fixed in
      `writer.py` (not `storage.py`/`retention.py`/`models.py` — see DECISIONS.md p06-implement/T7 for
      why that's still in scope) with a regression test in `tests/test_writer.py`; full PostgreSQL
      suite reverified clean 3/3 runs after the fix.
- [x] **T8** — `tests/test_storage.py`: `test_last_seen_never_moves_backwards` (the `Greatest` contract),
      green on both backends.
- [x] **T9** — `tests/test_toolchain.py`: the DSN-contract case tying `demo/docker-compose.yml`, `ci.yml`'s
      postgres job and `tests/settings.py:DEFAULT_PG_URL` together (section 3).
- [x] **T10** — Docs: `CHANGELOG.md` *Unreleased* entry (the PG pass, the sanitiser, any T7 fix, the new
      targets); `CLAUDE.md` command-table rows for `make test-pg` / `make pg-up` / `make pg-down`; a line
      in `.autodev/ARCHITECTURE.md` only if T7 changed behaviour.
      Amended in review round 1: T7 *did* change runtime behaviour (the writer thread now closes
      its connection unconditionally on stop, not only after the 60 s idle window) — corrected the
      original "no behaviour change" note and extended ARCHITECTURE.md's dependency-failure table
      and spec.md's connection-hygiene bullet accordingly (DECISIONS.md p06-review_fix1/writer).
- [x] **T11** — Final gate (see Verification), ending with `make pg-down` and a `docker ps` that shows no
      container.
      All verification commands run clean: PostgreSQL suite 213 passed (no warnings, 3 consecutive
      runs), SQLite suite 206 passed/7 skipped, ruff check/format clean, `django check` and
      `makemigrations --check` clean, `select_for_update(` call-form grep empty (see DECISIONS.md
      p06-implement/T11 for the one docstring-only literal-string hit), `make e2e-up` no-ops with exit
      0, `make test-pg` end-to-end leaves `docker ps` empty.

## Verification

```sh
# PostgreSQL
make pg-up
DJANGO_DB=postgres \
  ADMIN_ERRORS_TEST_PG_URL=postgres://postgres:postgres@localhost:5432/admin_errors_test \
  uv run pytest -q
make pg-down
docker ps            # must list no admin-errors container

# or, the single-command form
make test-pg

# SQLite (the gate)
uv run pytest -q
uv run ruff check . && uv run ruff format --check . \
  && uv run python -m django check --settings=tests.settings \
  && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings

grep -rn "select_for_update" src/ ; echo "exit=$?"   # expect no matches (exit=1)
make e2e-up          # expect: "e2e: demo/ not present yet, skipping", exit 0
```

| Acceptance criterion | Proved by |
|---|---|
| `docker compose ... up -d` plus the `DJANGO_DB=postgres` suite exits 0, full suite green | T4/T11 run of the commands above; `tests/test_postgres.py` is live (not skipped) in that run |
| Container brought down before the phase ends | T11 `make pg-down` followed by `docker ps`; `make test-pg` tears down even on a red run (T2) |
| `uv run pytest -q` (SQLite) stays green — no regression | T11 SQLite run; the `test_context.py` sanitiser case runs on both backends |
| Two-thread same-fingerprint race passes on PostgreSQL | `tests/test_storage.py::test_two_threads_storing_one_new_fingerprint_create_one_issue` under `DJANGO_DB=postgres` (T4) |
| Savepoint handling survives `IntegrityError` (PG-only path) | `tests/test_postgres.py::test_racing_issue_create_leaves_the_outer_transaction_usable` and `::test_racing_daily_count_create_leaves_the_outer_transaction_usable` (T5) |
| `grep -rn "select_for_update" src/` returns no matches | T11 grep, rerun after T6/T7 |
| `assertNumQueries` bounds in `test_storage.py` hold on PostgreSQL | the three `test_query_budget_*` cases in the T4/T11 PG run (not skipped there) |
| `demo/docker-compose.yml` exposes the documented DSN; CI postgres job verified against the same contract | the `tests/test_toolchain.py` DSN-contract case (T9), green on SQLite and PG |
| `make e2e-up` still exits 0 with the no-op message | T11 `make e2e-up` |
| CHANGELOG *Unreleased* entry | T10 |

## Risks

- **#5 PostgreSQL-only defects land late** — this phase *is* the mitigation and closes the row: the full
  suite runs on PG (T4), the race probe named in the risk runs there, and the two savepoint-isolation
  cases (T5) assert the specific behaviour ("outer transaction still usable after a swallowed
  `IntegrityError`") that SQLite cannot express.
- **#20 Unattended run stalls on an environment problem** — the Docker daemon (29.7.2) is up and no
  containers are running, checked at plan time. Two live hazards: (a) port 5432 already held by the
  machine's Postgres.app 17.4 — if `pg-up` fails with a bind error, the fallback is to run the suite
  against the already-listening server via `ADMIN_ERRORS_TEST_PG_URL` (superuser with CREATEDB needed)
  and record the exact error plus the fallback in DECISIONS.md, rather than killing an operator's
  server; (b) a leftover container — `test-pg` always tears down and T11 ends with `docker ps`.
- **#4 unbounded growth / long write locks** — untouched code, but the whole retention module runs
  against PostgreSQL for the first time (chunked deletes, `pk__in` parameter limits, the eviction loop),
  which is where a backend-specific limit would show.
- **#15 scope creep** — one predicted product change (the sanitiser) plus only what T4 forces; `demo/`
  gets its compose file and nothing else.

## Out of scope

- The rest of `demo/` — `manage.py`, `demo_project/`, `demo_app/`, `demo_seed`, the foreground
  `make demo` / `make demo-pg` targets (Phase 7).
- Admin UI, its list/detail `assertNumQueries` bounds and the browser pass (Phase 8).
- Notifications, status transitions, i18n (Phase 9).
- The migration squash, the second PG verification before release, README/docs and screenshots (Phase 10).
- MySQL/MariaDB: must not break on import, untested and unsupported (ARCHITECTURE.md line 42).
- PostgreSQL performance tuning (indexes beyond the shipped migration, connection pooling).
