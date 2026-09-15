# Phase 3 — Capture pipeline and storage (sync transport)

**Goal:** an error raised anywhere in a host project becomes an `Issue` row, with scrubbed context,
without the capture path ever raising or recursing.

## Context

What exists after Phase 2 (`f4d14b2`):

| File | State |
|---|---|
| `src/admin_errors/conf.py` | `settings` proxy, all 39 spec keys, cache cleared on `setting_changed`. `IN_APP_INCLUDE` stays `None` — the "default to `settings.BASE_DIR`" resolution was deferred to this phase (p02-plan/conf). |
| `src/admin_errors/models.py` | `Issue`, `Event`, `IssueDailyCount` with explicit index/constraint names and the `view_issue_context` permission. |
| `src/admin_errors/migrations/0001_initial.py` | The single shipped migration. **This phase adds no model fields, so it must not change.** |
| `src/admin_errors/fingerprint.py` | Frozen contract: `normalize_message`, `qualified_type_name`, `compute`, `for_exception(exc_type, culprit, message)`, `for_message(logger, level, template)`, `for_override(value)`. Takes an already-selected `culprit` — this phase supplies it. |
| `src/admin_errors/checks.py` | `W001`, `E002`, registered from `ready()`. |
| `src/admin_errors/apps.py` | `ready()` registers checks only. |
| `tests/settings.py` | Minimal host, `ROOT_URLCONF = "tests.settings"` with an empty `urlpatterns`, `ADMIN_ERRORS = {"TRANSPORT": "sync"}`. |
| `tests/` | `test_scaffold.py`, `test_toolchain.py`, `test_settings_and_checks.py`, `test_models.py`, `test_fingerprint.py` — pytest function style, `pytestmark = pytest.mark.django_db`. |

What this phase changes: everything between "an exception happens" and "a row exists". New modules
`context.py`, `capture.py`, `api.py`, `handlers.py`, `middleware.py`, `signals.py` (marker receiver
only), `storage.py`; `apps.ready()` gains handler installation and the marker connection. The test
host gains a real URLconf (`tests/urls.py`) with the views the capture tests need.

Nothing user-facing ships: there is still no admin UI, no writer thread, no retention.

## Design

### Module layout and responsibilities

```
handlers.AdminErrorsHandler ─┐
api.capture_exception/_message┤
integrations (Phase 5)       ├─► capture.capture(...)  ── never raises, never recurses
signals marker receiver ─────┘         │
middleware ContextVar ─────────────────┘
                                       │ guards → fingerprint → meta → context.build_payload
                                       │ → BEFORE_SEND → size enforcement
                                       ▼
                               CapturedItem(fingerprint, meta, timestamp, payload|None)
                                       │  TRANSPORT="sync" (this phase: the only path)
                                       ▼
                       storage.store_batch({fp: Aggregate}, using=alias)
```

**`context.py`** — the only module that touches untrusted host data. Public functions:

| Function | Contract |
|---|---|
| `safe_repr(value, *, limit) -> str` | `repr()` inside `try/except BaseException`; on failure `"<unrepresentable {type(value).__name__}>"` (and `"<unrepresentable>"` if even the class name lookup fails). Truncated to `limit` with a trailing `…` marker. |
| `select_culprit(tb) -> str` | Cheap traceback walk (`tb_frame.f_globals["__name__"]` + `f_code.co_name`), innermost **in-app** frame, else innermost frame overall. No `ExceptionReporter`. |
| `is_in_app(filename) -> bool` | `IN_APP_EXCLUDE` substrings win first; then `IN_APP_INCLUDE` prefixes (resolved to `[str(settings.BASE_DIR)]` when the setting is `None` and `BASE_DIR` is defined); if no prefixes are resolvable, every non-excluded frame is in-app. |
| `build_frames(request, exc_type, exc_value, tb) -> list[dict]` | `ExceptionReporter(...).get_traceback_frames()` — never `get_traceback_data()`. Keeps the innermost `MAX_FRAMES`. Each frame: `filename, lineno, function, module, in_app, pre_context, context_line, post_context`, plus `vars` (scrubbed by the reporter filter, each value through `safe_repr(limit=MAX_VAR_REPR_LENGTH)`) only when `CAPTURE_LOCALS`. |
| `build_exception_block(exc_value) -> dict` | `type`/`module`/`value` of the outermost exception plus `chain` (outermost first, `cause` flag, own frames) walking `__cause__`/`__context__`. |
| `build_request_block(request) -> dict \| None` | Per spec §7.2.6 via `get_exception_reporter_filter(request)`: `get_safe_request_meta` → header-case `HTTP_*`/`CONTENT_TYPE`/`CONTENT_LENGTH`, `get_post_parameters`, `get_safe_cookies`, `request.GET`, `build_absolute_uri()` guarded (`DisallowedHost`/any exception → `path` only), `remote_addr` from `REMOTE_ADDR`, `user` from `request.user` in its own `try/except` (anonymous → `None`), `body` only when `CAPTURE_REQUEST_BODY` (truncated to `MAX_BODY_BYTES`). |
| `build_extra(record) -> dict` | `LogRecord` attributes outside the standard set, values through `safe_repr`. |
| `build_payload(...) -> dict` | Assembles the spec §6.4 document with `"v": 1` and the `server` block (`hostname`, `pid`, `python`, `django`). Never settings, never environment. |

Every sub-block is individually wrapped: a failure in the request block must not cost the frames.

**`capture.py`** — the single chokepoint, spec §7.2 steps 1–3, 6, 7 (steps 4–5, admission and
sampling, are Phase 4). Public surface: `capture_exception_info(...)`, `capture_record(record)`,
`CapturedItem`. Structure:

1. `try/except BaseException` around the whole body; `KeyboardInterrupt`/`SystemExit` re-raised;
   everything else swallowed and, when `INTERNAL_LOGGING`, logged once per exception type per
   process to `admin_errors.internal` at WARNING.
2. Guards in cheapest-first order: recursion guard (`threading.local()` flag, set for the whole
   pipeline) → `ENABLED` → `DEBUG and not CAPTURE_IN_DEBUG` → logger name in `IGNORE_LOGGERS`
   (exact, or prefix when the entry ends with `.`) or under `admin_errors.` → `status_code` below
   `IGNORE_HTTP_STATUS_BELOW` → exception type in `IGNORE_EXCEPTIONS` (dotted paths resolved lazily
   and cached per process, subclasses included).
3. Once-per-exception marker: `getattr(exc, "__admin_errors_captured__", False)` → drop; else
   `setattr` inside `try/except` (a `__slots__` exception simply loses the dedup, never raises).
4. Fingerprint: explicit `fingerprint=` / `record.fingerprint` → `for_override`; exception →
   `for_exception(type(exc), culprit, str(exc))`; message-only → `for_message(logger, levelname, record.msg)`.
5. Meta (always cheap, always built — Phase 4 needs it for count-only items):
   `{"exception_type", "title", "culprit", "level", "logger"}`. `exception_type` is the short
   `type(exc).__qualname__` (the logger name for message-only records), `title` the formatted
   message, first line, ≤ 500 chars.
6. Payload via `context.build_payload`, then `BEFORE_SEND` (wrapped; raising → drop and count;
   returning `None` → drop), then size enforcement.
7. Dispatch: build `CapturedItem`, fold it into a one-key `dict[str, Aggregate]` and call
   `storage.store_batch(..., using=conf.settings.DATABASE)` inline.

**Size enforcement** (`MAX_PAYLOAD_BYTES`), re-measuring `len(json.dumps(payload).encode())` after
each step, stopping as soon as it fits: (1) drop `frames[*].vars` and every `chain[*].frames[*].vars`;
(2) drop `pre_context`/`post_context`/`context_line`; (3) truncate `message`, `exception.value` and
`title`; (4) last-resort minimal payload — `v`, `timestamp`, `level`, `logger`, `source`, truncated
`message`, `exception` without frames, and `"truncated": true`. Step 4 is an addition to spec §5's
three documented steps: the spec calls `MAX_PAYLOAD_BYTES` a *hard* cap, and steps 1–3 alone do not
bound a payload whose `request.post`/`headers` are large. Readers ignore unknown keys (ADR 0004).

**`api.py`** — the documented public surface (ADR 0008), a thin façade so internals can move:
`capture_exception(exc=None, *, request=None, extra=None, fingerprint=None, level="error") -> str | None`
(`exc=None` → `sys.exc_info()`; no active exception → `None`, nothing captured),
`capture_message(message, *, level="error", request=None, extra=None, fingerprint=None) -> str | None`,
`flush(timeout=2.0) -> None` (no-op under sync transport; Phase 4 wires the writer).

**`handlers.py`** — `AdminErrorsHandler(logging.Handler)`; `emit()` delegates to
`capture.capture_record(record)` and never calls `self.handleError()` (which would print to stderr on
an already-failing request). Installed from `ready()` on the root logger at `CAPTURE_LEVEL` when
`AUTO_INSTALL_LOGGING_HANDLER` and no `AdminErrorsHandler` instance is attached yet.

**`middleware.py`** — `RequestContextMiddleware` with `sync_capable = async_capable = True`, storing
the request in a module-level `ContextVar` and resetting the token in `finally`. Async support via
`asgiref.sync.iscoroutinefunction` + `markcoroutinefunction` (asgiref is already a Django
dependency — no new runtime dependency). `context.get_current_request()` reads it.

**`signals.py`** — this phase only holds the `got_request_exception` marker receiver:
it sets `exc.__admin_errors_request__ = request` from `sys.exc_info()` and never captures.
The `issue_created` / `issue_regressed` / `issue_status_changed` signals are Phase 4.

Request resolution order inside capture: explicit `request=` → `record.request` →
`exc.__admin_errors_request__` → the middleware `ContextVar` → `None`.

**`storage.py`** — `store_batch(batch: dict[str, Aggregate], *, using: str | None = None) -> None`,
spec §9.2, one `transaction.atomic(using=alias)` **per aggregate**:

1. `Issue.objects.using(alias).filter(fingerprint=fp).values("id", "status", "resolved_at", "notified_at").first()`.
2. Missing → create inside a nested `atomic()` savepoint (required on PostgreSQL); on `IntegrityError`
   re-run step 1. Creation sets `first_seen=first_ts`, `last_seen=first_ts`, `count=0`, meta fields.
3. Events: `Event.objects.bulk_create` from the samples, then trim the ring buffer — list the ids
   beyond `EVENTS_PER_ISSUE` ordered by `("-timestamp", "-id")`, then `filter(id__in=…).delete()`.
   Skipped entirely for a count-only aggregate and for an `ignored` issue.
4. One `Issue.objects.filter(pk=…).update(...)`: `count=F("count") + n`; `last_seen` assigned when
   `last_ts` is newer; `last_event` set to the newest sample payload; `status="open"` when the row
   was `resolved` (`resolved_at` is **kept**, so the UI can show *regressed*). No `select_for_update`.
5. Daily counts: for each UTC date in the aggregate, `UPDATE … count = F("count") + n`; 0 rows →
   create in a savepoint; on `IntegrityError` retry the update.

`ignored` issues get steps 1, 4 (counters and `last_seen` only) and 5 — no events, no status change.

### Deviation from ARCHITECTURE.md

`Aggregate` is defined in ARCHITECTURE.md as `(meta, count, first_ts, last_ts, samples)`. This phase
makes it a dataclass with a sixth field `dates: dict[datetime.date, int]` — occurrence counts keyed
by UTC date. Without it, an aggregate that spans UTC midnight cannot split its `count` across the two
`IssueDailyCount` rows that spec §9.2 step 6 requires: `first_ts`/`last_ts` give the two dates but not
how many occurrences fall on each. Appended to DECISIONS.md. `CapturedItem` keeps the documented
shape and lives in `capture.py`; `Aggregate` lives in `storage.py` (the consumer owns its input type;
Phase 4's `writer.py` imports it).

`MAX_FRAMES` and `MAX_VAR_REPR_LENGTH` are listed in the roadmap under `capture.py` but are
implemented in `context.py`, where the frames and var reprs are actually built. `capture.py` owns
`MAX_PAYLOAD_BYTES` and the degradation order. Appended to DECISIONS.md.

### Error handling

No code path in `capture.py`, `context.py`, `handlers.py`, `middleware.py` or the marker receiver may
propagate an exception to the host. `storage.store_batch` *is* allowed to raise — Phase 4's writer
swallows it — but the sync dispatch in `capture.py` sits inside the pipeline's own
`try/except BaseException`, which is exactly what the "monkeypatched `store_batch` raises" acceptance
criterion tests. The recursion guard stays set while the pipeline runs, so an error logged by our own
code during capture is dropped rather than re-entering.

### Test host changes

`tests/urls.py` becomes `ROOT_URLCONF`, with the views the capture cases need: `/boom/` (`ValueError`),
`/nested/` (chained), `/notfound/` (`Http404`), `/denied/` (`PermissionDenied`), `/log-and-raise/`,
`/log-only/`, `/sensitive-post/` (`@sensitive_post_parameters`), `/sensitive-vars/`
(`@sensitive_variables`), `/unrepresentable/`, `/broken-user/`, `/aboom/` (async). Views live in
`tests/views.py`. `MIDDLEWARE` stays as it is; the cases that need `RequestContextMiddleware` add it
with `override_settings`. `conftest.py` gains an autouse fixture resetting the recursion guard and the
internal-logging dedup set between tests.

## Tasks

- [x] T1: Test host — `tests/views.py` + `tests/urls.py` with the views listed above, `ROOT_URLCONF`
      switched in `tests/settings.py`, autouse reset fixture in `tests/conftest.py`. Existing suite
      stays green (`test_settings_and_checks.py` uses the admin URLconf indirectly).
- [x] T2: `context.py` part 1 — `safe_repr`, `is_in_app` (incl. the `BASE_DIR` resolution),
      `select_culprit`, `build_frames` (`ExceptionReporter.get_traceback_frames`, `MAX_FRAMES`,
      `MAX_VAR_REPR_LENGTH`, `CAPTURE_LOCALS`), `build_exception_block` with `chain`.
- [x] T3: `context.py` part 2 — `build_request_block` (reporter-filter delegation, `DisallowedHost`
      fallback, user block, body), `build_extra`, `build_payload` with the `server` block,
      `get_current_request()`.
- [x] T4: `tests/test_context.py` — scrubbing (POST `password`, `@sensitive_post_parameters`,
      `@sensitive_variables` locals, `Authorization`/`Cookie`/`X-Api-Key` headers, `sessionid`
      cookie, all asserted **absent** from the serialized payload), raising `__repr__` → fallback,
      `DisallowedHost` → path, anonymous vs authenticated user, raising `request.user`, `in_app`
      include/exclude, chained exceptions → `chain` entries.
- [x] T5: `capture.py` part 1 — `CapturedItem`, the `try/except BaseException` shell, recursion guard,
      guards (`ENABLED`, `DEBUG`/`CAPTURE_IN_DEBUG`, `IGNORE_LOGGERS`, `status_code`,
      `IGNORE_EXCEPTIONS`), once-per-exception marker, fingerprint selection, meta build,
      request resolution order, internal logging once per exception type.
- [x] T6: `capture.py` part 2 — `BEFORE_SEND` (wrapped; mutate and drop), size enforcement and the
      four-step degradation order, sync dispatch folding a `CapturedItem` into a one-key batch.
- [x] T7: `storage.py` — `Aggregate`, `store_batch` with the five steps above; savepointed racing
      create; `ignored`/`resolved` handling; ring-buffer trim; daily counts per UTC date.
- [x] T8: `tests/test_storage.py` — create and update paths, counters, `last_seen`, `first_seen`
      unchanged, ring buffer keeps exactly `EVENTS_PER_ISSUE` newest, `last_event` updated, midnight
      batch → two `IssueDailyCount` rows, resolved → open keeping `resolved_at`, ignored → counters
      only, and the three `django_assert_max_num_queries` bounds. No concurrency/race test in this
      phase (PLAN's own task list omits it; spec defers the two-thread `IntegrityError` race case to
      the writer-thread phase). `assertNumQueries` bounds are generous placeholders (not yet tightened
      to the observed counts noted in the comments) — worth revisiting in T12's polish pass.
- [x] T9: `api.py`, `handlers.py`, `middleware.py`, `signals.py` marker receiver, and `apps.ready()`
      wiring (handler installation + `got_request_exception.connect`; still no DB, no thread, no
      filesystem).
- [x] T10: `tests/test_capture.py` part 1 — the happy paths: unhandled view exception via
      `Client(raise_request_exception=False)` → exactly one Issue, `source="exception"`,
      `logger="django.request"`, request block present; `logger.exception` in a view with and without
      the middleware; `capture_message` → `source="message"` and `extra` stored;
      `capture_exception()` with no active exception → `None`; async view under `AsyncClient` with
      request context.
- [x] T11: `tests/test_capture.py` part 2 — the drop and safety paths: `Http404`/`PermissionDenied`,
      `IGNORE_LOGGERS` exact and prefix, `status_code < 500`, log-and-re-raise → one occurrence,
      `ENABLED=False`, `CAPTURE_IN_DEBUG=False`, `BEFORE_SEND` mutate and drop, monkeypatched
      `store_batch` raising (request still returns, no recursion, nothing propagates),
      `MAX_FRAMES`/`MAX_VAR_REPR_LENGTH`/payload degradation order.
- [x] T12: `CHANGELOG.md` *Unreleased* entry; run the full lint command and `uv run pytest -q`;
      confirm `makemigrations --check` still reports no changes. Lint debt from the previous session
      (17 `E501` docstring/comment wraps) cleared by hand plus `uv run ruff format .`; full lint
      command and `uv run pytest -q` (105 passed) both green.

## Handoff (session 1 → session 2)

Done and green: T1–T9 (test host, `context.py`, `storage.py`, `capture.py`, `api.py`/`handlers.py`/
`middleware.py`/`signals.py`/`apps.py` wiring) plus their tests T4 and T8. `uv run pytest -q` passes
(87 tests). Not yet started: T10, T11 (`tests/test_capture.py`, both parts — the module does not
exist yet) and T12 (lint cleanup + `CHANGELOG.md` + final full-suite/lint run). Pick up at T10: it
exercises `capture.py`/`api.py`/`handlers.py` through `tests/views.py` (already built) via
`Client(raise_request_exception=False)` and `AsyncClient`. Read `capture.py` top-to-bottom first —
the guard order, `_run_capture`/`_capture_record`/`_capture_exception`/`_capture_message` split, and
`_enforce_size` are all already implemented and were not exercised by any test yet in this session.

## Verification

```
uv run pytest -q
uv run ruff check . && uv run ruff format --check . \
  && uv run python -m django check --settings=tests.settings \
  && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings
```

| Acceptance criterion | Test that proves it |
|---|---|
| Unhandled view exception → exactly one Issue, `source="exception"`, `logger="django.request"`, request block | `test_capture.py::test_unhandled_view_exception_creates_one_issue_with_request_context` |
| POST `password` scrubbed | `test_context.py::test_post_password_value_is_absent_from_the_payload` |
| `@sensitive_post_parameters` scrubbed | `test_context.py::test_sensitive_post_parameters_values_are_absent_from_the_payload` |
| `@sensitive_variables` locals scrubbed | `test_context.py::test_sensitive_variables_locals_are_absent_from_the_payload` |
| `Authorization` / `Cookie` / `X-Api-Key` headers scrubbed | `test_context.py::test_sensitive_headers_are_absent_from_the_payload` |
| `sessionid` cookie scrubbed | `test_context.py::test_sessionid_cookie_value_is_absent_from_the_payload` |
| Raising `store_batch` does not raise or recurse | `test_capture.py::test_storage_failure_does_not_propagate_or_recurse` |
| Raising `__repr__` → fallback string | `test_context.py::test_unrepresentable_object_becomes_the_fallback_string` |
| Raising `request.user` does not break capture | `test_context.py::test_broken_request_user_does_not_break_the_payload` |
| `Http404` / `PermissionDenied` dropped | `test_capture.py::test_ignored_exception_types_are_dropped` |
| `IGNORE_LOGGERS` exact and prefix | `test_capture.py::test_ignore_loggers_exact_and_prefix` |
| `status_code < 500` dropped | `test_capture.py::test_records_below_the_status_threshold_are_dropped` |
| Log-and-re-raise → one occurrence | `test_capture.py::test_log_and_reraise_produces_one_occurrence` |
| `ENABLED=False`, `CAPTURE_IN_DEBUG=False` are no-ops | `test_capture.py::test_enabled_false_is_a_noop`, `::test_capture_in_debug_false_is_a_noop` |
| Ring buffer keeps exactly `EVENTS_PER_ISSUE` newest | `test_storage.py::test_event_ring_buffer_keeps_the_newest_events_per_issue` |
| `first_seen` unchanged on update | `test_storage.py::test_first_seen_is_unchanged_on_update` |
| Midnight batch → two `IssueDailyCount` rows | `test_storage.py::test_batch_spanning_utc_midnight_creates_two_daily_rows` |
| Resolved issue reopens keeping `resolved_at` | `test_storage.py::test_resolved_issue_reopens_and_keeps_resolved_at` |
| Ignored issue gets counters only | `test_storage.py::test_ignored_issue_gets_counters_only` |
| `assertNumQueries` bounds (new issue + 1 sample ≤ 16, count-only ≤ 8, 3 samples ≤ 10) | `test_storage.py::test_query_budget_new_issue_with_one_sample`, `::test_query_budget_count_only`, `::test_query_budget_existing_issue_with_three_samples` (via `django_assert_max_num_queries`) |
| Async view under `AsyncClient` captured with request context | `test_capture.py::test_async_view_exception_is_captured_with_request_context` |
| Lint exits 0 | T12 |

Bounds are upper bounds, chosen to leave room for savepoint statements on both backends while still
failing on a per-row insert loop or a per-date N+1; the implementer records the observed numbers in a
comment next to each bound.

## Risks

| Risk | What this plan does |
|---|---|
| #2 Secret/PII leak through a payload | Scrubbing is never hand-rolled: `get_exception_reporter_filter(request)` for locals, POST and cookies; `get_safe_request_meta` for headers. Five absence assertions on the serialized payload (T4). Settings and environment are never collected — `get_traceback_data()` is explicitly not called. Closes the capture half of #2; the render half stays with Phase 8. |
| #3 Capture raises, recurses or hangs | One `try/except BaseException` shell with `KeyboardInterrupt`/`SystemExit` re-raised, thread-local recursion guard, safe `repr()`, per-block wrapping in `context.py`, wrapped `BEFORE_SEND`, `emit()` that never calls `handleError`. Three explicit tests (T11, T4). Closes #3 for the synchronous path; the writer-thread flag is Phase 4. |
| #5 PostgreSQL-only defects land late | `storage.py` is written to the PG rules from the first line: every racing create (issue and daily count) inside its own `atomic()` savepoint, `IntegrityError` → re-read, no `select_for_update` anywhere. The dedicated PG pass (Phase 6) then only has to confirm. |
| #7 Grouping wrong in practice | `select_culprit` is the only new input to the frozen fingerprint; it is a separate, testable function, and the "same message, two culprits" behaviour already has Phase 2 golden tests. |
| #9 N+1 on the storage path | The three `assertNumQueries` bounds (T8) pin the per-aggregate budget before the admin ever reads these rows. |
| #12 Capture silently disabled | `AUTO_INSTALL_LOGGING_HANDLER` wiring plus the `got_request_exception` marker fallback land here; `W002` and `errors_test` stay with Phase 5. |
| #16 Coverage gamed / tests loosened | Every deliverable has a named test in the table above; no `pragma: no cover` on capture or storage paths. |

## Out of scope

- `writer.py`, the thread transport, `api.flush` actually blocking, `atexit`, pid handling — Phase 4.
- Sampling (`EVENT_SAMPLE_PER_HOUR`), new-issue admission (`NEW_ISSUES_PER_MINUTE`), the
  `dropped_*` stats counters and count-only items produced by *capture* — Phase 4. `storage.py` already
  accepts a count-only aggregate (it is what the count-only query-budget test uses).
- `signals.issue_created` / `issue_regressed` / `issue_status_changed` and `on_commit` firing — Phase 4.
  This phase performs the `resolved → open` transition but emits no signal.
- `retention.py`, `routers.py`, `tasks.py`, `integrations/celery.py`, the management commands, checks
  `W002`/`W003`/`E001` — Phase 5.
- The PostgreSQL run — Phase 6. `demo/` — Phase 7. Admin UI, templates, permission rendering — Phase 8.
- Notifications and i18n of anything added here — Phase 9. README and docs — Phase 10.
