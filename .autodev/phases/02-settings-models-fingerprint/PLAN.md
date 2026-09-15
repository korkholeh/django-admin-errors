# Phase 2 — Settings proxy, models and fingerprint

**Goal:** pin the two contracts that are expensive to change later — the database schema (exactly one
migration) and the fingerprint algorithm (frozen sha1, golden-value tested).

## Context

**What exists after Phase 1** (commit `cc03ee9`):

- `src/admin_errors/__init__.py` — `__version__ = "0.1.0.dev0"`, no side effects.
- `src/admin_errors/apps.py` — `AdminErrorsConfig` (`label="admin_errors"`, `verbose_name=_("Errors")`,
  `default_auto_field="django.db.models.BigAutoField"`) with an empty `ready()` carrying the
  "no DB, no thread, no filesystem" docstring.
- `src/admin_errors/migrations/__init__.py` — empty, so `makemigrations --check` already has a target.
- `tests/settings.py` — minimal host (`contenttypes`, `auth`, `sessions`, `messages`, `admin`,
  `admin_errors`), SQLite `:memory:` by default, Postgres via `DJANGO_DB=postgres`,
  `USE_TZ=True`, `TIME_ZONE="UTC"`, `ADMIN_ERRORS = {"TRANSPORT": "sync"}`.
- `tests/conftest.py` — one `repo_root` fixture. `tests/test_scaffold.py`, `tests/test_toolchain.py`
  (14 tests, green). `pytest-django` is wired through `[tool.pytest.ini_options]`.
- Lint already runs `django check --settings=tests.settings` and
  `makemigrations admin_errors --check --dry-run`, so both are load-bearing from this phase on.

**What this phase changes** — pure addition, no existing file is rewritten except `apps.py` (`ready()`
gains two `register()` calls) and `CHANGELOG.md`:

| File | Kind |
|---|---|
| `src/admin_errors/conf.py` | new — defaults + `settings` proxy |
| `src/admin_errors/checks.py` | new — `W001`, `E002` |
| `src/admin_errors/models.py` | new — `Issue`, `Event`, `IssueDailyCount` |
| `src/admin_errors/migrations/0001_initial.py` | new — the only migration 0.1.0 ships |
| `src/admin_errors/fingerprint.py` | new — frozen sha1 algorithm |
| `src/admin_errors/apps.py` | edit — `ready()` registers the checks |
| `tests/test_settings_and_checks.py`, `tests/test_models.py`, `tests/test_fingerprint.py` | new |
| `CHANGELOG.md` | edit — *Unreleased* entry |

**Sources of truth:** spec §5 (settings table), §6.1–6.3 (models), §8 (fingerprint), §11.5 (checks),
§12.5 (permissions), §14.2 (`test_fingerprint.py` cases), §16 (one migration, no `select_for_update`);
ADR 0003 (fingerprint is a frozen public contract), ADR 0008 (one migration);
`ARCHITECTURE.md` module table rows for `conf.py` / `checks.py` / `models.py` / `fingerprint.py` and the
data-model section. No deviation from `ARCHITECTURE.md` is planned.

## Design

### `conf.py` — the settings proxy

```python
DEFAULTS: dict[str, object] = { "ENABLED": True, ... }   # all 38 keys of spec §5, verbatim defaults

class Settings:
    def __getattr__(self, name: str) -> Any: ...   # resolved dict, cached; AttributeError on unknown
    def as_dict(self) -> dict[str, Any]: ...       # merged view, used by tests
    def unknown_keys(self) -> list[str]: ...       # host keys not in DEFAULTS — checks.W001 uses it
    def reset(self) -> None: ...                   # drop the cache

settings = Settings()                              # the single module-level instance
setting_changed.connect(_on_setting_changed)       # at import; clears the cache
```

- Resolution is `DEFAULTS | getattr(django.conf.settings, "ADMIN_ERRORS", {})`, computed once and
  cached in `Settings._cache`. Unknown host keys are kept in the merged dict (so a typo does not crash
  a host) but reported by `W001`; attribute access to a name that is not in `DEFAULTS` raises
  `AttributeError` so *our own* typos fail loudly.
- `_on_setting_changed` clears the cache on **every** `setting_changed` signal, not only
  `setting=="ADMIN_ERRORS"`. The signal fires only under `override_settings`/`modify_settings`, the
  reset is a dict assignment, and this also covers `DEBUG`/`BASE_DIR` overrides that other phases read
  through the same proxy. Cheap and impossible to get subtly wrong.
- `IN_APP_INCLUDE` stays `None` in the proxy. Spec §5 says the *default* is `settings.BASE_DIR` "if
  defined"; that resolution needs a filesystem-shaped value and only `context.py` consumes it, so it
  belongs to Phase 3, next to `IN_APP_EXCLUDE` matching. The proxy stays a dumb dict view.
- Mutable defaults (`IGNORE_LOGGERS`, `IGNORE_EXCEPTIONS`, `IN_APP_EXCLUDE`, `NOTIFY_ON`) are shared
  list objects. Callers must not mutate them; nothing in the codebase does. Documented in the module
  docstring rather than defended with per-read copies.
- Import-time `setting_changed.connect(...)` is an import side effect in `conf.py`, which is allowed —
  the ban in `CLAUDE.md`/PROFILE applies to `__init__.py`. `conf.py` imports only `django.conf`,
  `django.test.signals`; it never imports models, so importing it from `checks.py` at `ready()` time is
  registry-safe.

### `checks.py`

```python
def check_settings_keys(app_configs, **kwargs) -> list[CheckMessage]   # admin_errors.W001
def check_sqlite_version(app_configs, **kwargs) -> list[CheckMessage]  # admin_errors.E002
```

- `W001`: one `Warning` per unknown key, sorted, `id="admin_errors.W001"`, `hint` naming the closest
  valid key is out of scope — the message lists the key and points at spec §5.
- `E002`: reads `sqlite3.sqlite_version_info` from the stdlib module at call time (so a test can patch
  `sqlite3.sqlite_version_info`) and only when the configured alias
  (`conf.settings.DATABASE`) exists in `django.conf.settings.DATABASES` **and** its `ENGINE` ends with
  `sqlite3`. It never opens a connection — checks must stay DB-free. A missing alias is `E001`,
  Phase 3's deliverable; here it is simply skipped.
- Both are registered from `AdminErrorsConfig.ready()` via
  `django.core.checks.register(fn, Tags.compatibility)` — not with a module-level decorator — so
  importing `admin_errors.checks` has no global effect and `ready()` stays free of DB/thread/filesystem
  work. `ready()` imports `checks` locally inside the method, matching the Django idiom for signal and
  check wiring.

### `models.py`

Exactly spec §6.1–6.3. `TextChoices` with `gettext_lazy` labels for `level` and `status`; every
`verbose_name` translatable, ready for Phase 8/9.

```
Issue    fingerprint CharField(40, unique)      exception_type CharField(200, db_index)
         title CharField(500)                   culprit CharField(500, blank)
         level CharField(10, choices, default=ERROR)
         status CharField(10, choices, default=OPEN, db_index)
         first_seen / last_seen DateTimeField (last_seen db_index)
         count PositiveBigIntegerField(default=0)
         resolved_at DateTimeField(null)        resolved_by FK(AUTH_USER_MODEL, null, SET_NULL)
         notified_at DateTimeField(null)        last_event JSONField(null)
  Meta   ordering ["-last_seen"]; indexes [("status", "-last_seen")]
         permissions [("view_issue_context", "Can view request context and local variables")]

Event    issue FK(Issue, CASCADE, related_name="events")
         timestamp DateTimeField(db_index)      payload JSONField
  Meta   indexes [("issue", "-timestamp")]

IssueDailyCount  issue FK(Issue, CASCADE, related_name="daily_counts")
                 date DateField                 count PositiveIntegerField(default=0)
  Meta   constraints [UniqueConstraint(("issue", "date"), name=...)]; indexes [("date",)]
```

- Every index and constraint gets an **explicit** name (`ae_issue_status_seen_idx`,
  `ae_event_issue_ts_idx`, `ae_daily_date_idx`, `ae_daily_issue_date_uniq`; all ≤ 30 chars). Django's
  auto-names are deterministic but are derived from the table name, so an explicit name survives the
  0.1.0 squash and any later `db_table` change without a rename migration (risk #19).
- `resolved_by` references `settings.AUTH_USER_MODEL` by string, never by import.
- `__str__` on each model; no business logic, no manager overrides, no `select_for_update` anywhere —
  `storage.py` (Phase 3) owns all write behaviour.
- No field stores settings or environment (spec §16); `last_event`/`payload` hold only §6.4 payloads.
- Postgres-safe by construction: `JSONField` maps to `jsonb`, `PositiveBigIntegerField` to `bigint`
  with a check constraint, and the `UniqueConstraint` is what makes the savepointed upsert in Phase 3
  work on both backends (risk #5).

### `migrations/0001_initial.py`

Generated with `makemigrations admin_errors` after the models land, then read and, if needed,
hand-tidied (name `0001_initial`, dependency on `migrations.swappable_dependency(settings.AUTH_USER_MODEL)`).
Nothing else ships. `makemigrations --check --dry-run` becomes green again and is additionally asserted
from inside the suite so the gate, not only the lint command, catches a model change without a migration.

### `fingerprint.py` — the frozen contract

```python
UUID_RE, ISO8601_RE, ADDR_RE, HEX_RE, QUOTED_RE, DIGITS_RE, WS_RE   # module constants, frozen
MAX_MESSAGE_LENGTH = 200

def compute(parts: Sequence[str]) -> str                    # sha1(":".join(parts)).hexdigest()
def normalize_message(message: str) -> str
def qualified_type_name(exc_type: type[BaseException]) -> str   # f"{__module__}.{__qualname__}"
def for_exception(exc_type, culprit: str, message: str) -> str
def for_message(logger: str, level: str, template: object) -> str
def for_override(value: str) -> str                         # compute([str(value)])
```

Frozen normalization order (a comment in the module states that changing it is a major-version
breaking change and points at ADR 0003):

1. first line only, `strip()`
2. UUIDs → `<uuid>`
3. ISO-8601 dates/datetimes → `<ts>`  *(before the hex and digit rules, which would otherwise eat them)*
4. `0x…` addresses → `<addr>`  *(before the generic hex rule)*
5. hex runs ≥ 8 chars → `<hex>`
6. single- and double-quoted literals → `<str>`
7. remaining digit runs → `#`
8. whitespace collapsed to single spaces
9. truncate to 200 chars

- `for_exception` takes the **outermost** exception's type, the already-computed `culprit` and
  `str(exc)`; frame selection and in-app detection stay in `context.py`/`capture.py` (Phase 3). This
  keeps the frozen part dependency-free: `fingerprint.py` imports only `hashlib`, `re`, `typing`.
- `qualified_type_name` always emits `module.QualName` including `builtins.ValueError` — no special
  case for builtins, because a special case is one more thing that can never be changed. The short name
  that `Issue.exception_type` displays is computed by the caller, not here.
- `for_message` normalizes only when `record.msg` is not a `str` (spec §8.2: the template is already
  the grouping key). Parts are `[logger, levelname, template]`.
- Overrides are hashed verbatim (`for_override`), so an override and a natural fingerprint can never be
  told apart downstream — the column stays a plain sha1 hex.

**Error handling.** Nothing in this phase may raise on hostile input: `normalize_message` accepts any
`str` including empty and multi-line; `for_message` coerces a non-string `msg` through `str()`;
`compute` joins with `":"` and encodes UTF-8 with `errors="replace"`. None of these modules do I/O, so
there is no failure mode to swallow — the never-raise wrapper lives in `capture.py` (Phase 3).

### Case selection (guide: `case-taxonomy.md`)

Kept for this phase — happy path (exception / message / override fingerprints, defaults resolve),
input & boundaries (empty, multi-line, > 200 chars, unicode, non-`str` `record.msg`, each
normalization family), errors (unknown setting key → `W001`, patched SQLite 3.8 → `E002`, unknown
attribute on the proxy → `AttributeError`), permissions (`view_issue_context` present next to the four
defaults after `migrate`), idempotency/repetition (same inputs → same hash; the proxy reverts after
`override_settings` exits), persistence (schema is what `migrate` produces — asserted through the
`Permission` rows and `makemigrations --check`).

Deferred, `deferred_not_authored`: concurrency (no write path until Phase 3's `store_batch`),
navigation/lifecycle, async chains, accessibility, platform sanity — this phase has no user-facing
surface and no I/O.

## Tasks

- [x] **T1: `conf.py` — defaults and proxy.** New `src/admin_errors/conf.py` with `DEFAULTS` holding all
  spec §5 keys verbatim, the `Settings` class (`__getattr__`, `as_dict`, `unknown_keys`, `reset`), the
  module-level `settings` instance and the `setting_changed` receiver. No tests yet.
- [x] **T2: settings tests.** New `tests/test_settings_and_checks.py` (settings half): every default in
  `DEFAULTS` matches the spec §5 table (a literal expected table in the test file, not a re-derivation
  from `DEFAULTS`); `conf.settings.ENABLED is True` under the test host; inside
  `override_settings(ADMIN_ERRORS={"EVENTS_PER_ISSUE": 3})` the proxy reads `3` and every other key still
  falls back to its default, and after the block it is back to `20`; unknown attribute raises
  `AttributeError`; `unknown_keys()` is empty for the test host's `{"TRANSPORT": "sync"}`.
- [x] **T3: `checks.py` + registration.** New `src/admin_errors/checks.py` with `check_settings_keys`
  (`admin_errors.W001`) and `check_sqlite_version` (`admin_errors.E002`); `apps.py` `ready()` imports the
  module locally and calls `register()` for both, keeping `ready()` DB-, thread- and filesystem-free.
- [x] **T4: checks tests.** Extend `tests/test_settings_and_checks.py`: `run_checks()` on the default test
  settings returns no message with an `admin_errors.` id; `override_settings(ADMIN_ERRORS={"NOPE": 1})`
  yields exactly one `admin_errors.W001` naming `NOPE`; patching `sqlite3.sqlite_version_info` to
  `(3, 8, 3)` yields `admin_errors.E002` on the SQLite host and nothing when the configured alias is
  Postgres (skip-on-`DJANGO_DB=postgres` handled by asserting on the alias's real engine, so the test is
  meaningful on both backends).
- [x] **T5: `models.py`.** New `src/admin_errors/models.py` with `Issue`, `Event`, `IssueDailyCount`
  exactly as the Design table, explicit index/constraint names, `TextChoices`, translatable verbose
  names, `__str__`.
- [x] **T6: the single migration.** Run `uv run python -m django makemigrations admin_errors
  --settings=tests.settings`, review the generated `0001_initial.py`, confirm the swappable dependency and
  that it is the only file besides `__init__.py`.
- [x] **T7: model and schema tests.** New `tests/test_models.py`: after `migrate`, `Permission` for
  `admin_errors.issue` contains `view_issue_context` plus `add/change/delete/view_issue`; `Issue.Meta`
  ordering, the `("status", "-last_seen")` index, the `Event` index and the `IssueDailyCount`
  unique constraint are present in `Meta` **and** enforced by the database (creating two
  `IssueDailyCount` rows with the same `(issue, date)` raises `IntegrityError` inside its own
  `transaction.atomic()`); a duplicate `fingerprint` raises `IntegrityError`;
  `call_command("makemigrations", "admin_errors", check=True, dry_run=True, verbosity=0)` exits without
  raising; the migrations directory holds exactly one migration module.
- [x] **T8: `fingerprint.py`.** New `src/admin_errors/fingerprint.py` with the regex constants, the frozen
  nine-step `normalize_message`, `compute`, `qualified_type_name`, `for_exception`, `for_message`,
  `for_override`, and the "changing this is a breaking change (ADR 0003)" module docstring.
- [x] **T9: `tests/test_fingerprint.py` with golden values.** Every spec §14.2 case: three golden hex
  literals (one exception, one message-only, one override) pinned in the file and computed from the real
  implementation at authoring time, plus one test spelling out the exact `":"`-joined parts string for the
  exception case so the pin is readable; normalization per family (digits, UUID, hex, `0x` address,
  ISO-8601 timestamp, quoted literal, collapsed whitespace) as parametrized input→expected pairs; same
  exception+message from two culprits → two fingerprints; two exception types with one message → two;
  `record.msg` template beats `record.getMessage()` (two records, different args, one fingerprint);
  non-`str` `record.msg` normalized through `str()`; explicit override wins over natural inputs;
  determinism (same inputs twice); boundaries (empty message, multi-line takes the first line only,
  a > 200-char message truncates to 200, unicode survives).
- [x] **T10: changelog and full verification.** `CHANGELOG.md` *Unreleased* entry; run the whole
  Verification table below and record the output.

## Verification

```bash
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run python -m django check --settings=tests.settings
uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings
ls src/admin_errors/migrations/
```

| Acceptance criterion | Proven by |
|---|---|
| `uv run pytest -q` green | the gate command itself |
| `test_fingerprint.py` covers §14.2 incl. pinned golden hashes | `tests/test_fingerprint.py::test_golden_*` (T9) |
| both normalization families (exception message + message template) | `tests/test_fingerprint.py::test_normalizes_*` (parametrized) and `::test_message_records_use_the_raw_template` |
| same message, two culprits → two fingerprints | `tests/test_fingerprint.py::test_same_message_two_culprits_do_not_merge` |
| two types, one message → two fingerprints | `tests/test_fingerprint.py::test_two_exception_types_one_message_do_not_merge` |
| message-template keying | `tests/test_fingerprint.py::test_message_records_use_the_raw_template` |
| `fingerprint=` override wins | `tests/test_fingerprint.py::test_explicit_override_wins` |
| `makemigrations --check --dry-run` exits 0 | the lint command **and** `tests/test_models.py::test_models_have_no_pending_migrations` |
| exactly one migration besides `__init__.py` | `tests/test_models.py::test_exactly_one_migration_ships` + `ls` |
| `Permission` has `view_issue_context` + the four defaults | `tests/test_models.py::test_migrate_creates_the_issue_permissions` |
| `conf.settings` reflects and reverts `override_settings` | `tests/test_settings_and_checks.py::test_override_settings_is_visible_and_reverts` |
| `django check` emits no warnings on the default test settings | the lint command **and** `tests/test_settings_and_checks.py::test_default_test_settings_produce_no_admin_errors_messages` |
| targeted W001 | `tests/test_settings_and_checks.py::test_unknown_setting_key_raises_w001` |
| targeted E002 (patched SQLite version) | `tests/test_settings_and_checks.py::test_old_sqlite_raises_e002` |

Test command is unchanged: `uv run pytest -q`.

## Risks

| Risk | What this phase does |
|---|---|
| **#7 grouping is wrong in practice** (closed here per the roadmap) | The algorithm is isolated in a dependency-free module, the normalization order is frozen and commented, and T9 pins three golden hashes plus every normalization family, both under-merge probes (two culprits, two types) and the over-merge escape hatch (`fingerprint=`). |
| **#19 migration churn** | One `makemigrations` run, reviewed; `tests/test_models.py` asserts both "no pending migrations" and "exactly one migration module", so a later phase adding a second one fails the gate rather than the release. Explicit index/constraint names keep the 0.1.0 squash rename-free. |
| **#12 capture silently disabled** (partial: `W001`/`E002` half) | `W001` catches typo'd keys the moment the host runs `manage.py check`; `E002` catches a SQLite build without JSON1 before the first `JSONField` write fails at runtime. `W002`/`E001`/`W003` stay with Phases 3 and 5, where the logging and alias code they describe lands. |
| **#5 PostgreSQL-only defects** | Schema is written to the PG rules now: the `(issue, date)` `UniqueConstraint` and the unique `fingerprint` are what make Phase 3's savepointed racing create safe; no `select_for_update` anywhere; field types chosen to map cleanly to `jsonb`/`bigint`. T7's `IntegrityError` assertions each run inside their own `transaction.atomic()` so they do not abort the surrounding test transaction on PG. |
| **#2 secret leak** | Negative contribution only: no model field and no fingerprint part may hold settings or environment (spec §16); the `view_issue_context` permission that Phase 8 gates on is created here. |
| **#16 coverage gamed** | All four new modules are small and fully exercised by the three new test modules; no `pragma: no cover`, no skips. |

## Out of scope

- `context.py`, `capture.py`, `api.py`, `handlers.py`, `middleware.py`, `storage.py` and the
  `ready()`-time logging-handler installation — Phase 3.
- Resolving `IN_APP_INCLUDE` to `settings.BASE_DIR` and the `IN_APP_EXCLUDE` matching — Phase 3
  (`context.py`), where in-app detection is consumed.
- Checks `W002` (logging propagation), `E001` (missing DB alias), `W003` (`mail_admins` overlap) —
  Phases 3 and 5, with the code they describe.
- `writer.py`, sampling and admission buckets — Phase 4. `retention.py`, `routers.py`, management
  commands, Celery — Phase 5.
- `admin.py`, templates, static assets, any permission *enforcement* — Phase 8. Signals and
  notifications — Phases 4 and 9. i18n catalogue — Phase 9.
- The migration squash and the `0.1.0` version bump — Phase 10.
