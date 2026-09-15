# Review — phase 5 round 1

**Verdict:** changes_requested

Phase 5 delivers all 15 planned tasks and the gate genuinely passes — I re-ran it: `uv run pytest -q` 196 passed / 1 skipped, ruff check + format, `django check` and `makemigrations --check` clean, and `uv run tox -e celery` exit 0. Every acceptance criterion has a named test except the two gaps below, and the design work (portable NULL-safe rule 3 predicate, timestamp-before-run in the opportunistic hook, the router's deliberate non-opinion on the user model, the `find_spec` Celery gate) is well reasoned and logged in DECISIONS.md with no unproven environment excuses. One real defect blocks approval: eviction (rule 6) selects `total - target` primary keys into a single `pk__in` list, so `run_cleanup()` raises `OperationalError: too many SQL variables` on any large overflow — reproduced on this branch with 40 000 issues and `MAX_ISSUES=5000`. That is the failure mode rule 6 exists to prevent (risk 18), it breaks `errors_cleanup` and the Celery task, and the existing 12-row eviction test cannot see it. The rest are minor: the full-VACUUM path is untested at `auto_vacuum=2` (the criterion names both values), the PostgreSQL size query binds a table name where `regclass` is required, `--vacuum --dry-run` warns about a VACUUM it will not run, and two assertions are weaker than the criteria they stand for.

## [MAJOR] Eviction builds one unbounded `pk__in` list — `run_cleanup()` raises `OperationalError: too many SQL variables` on a large overflow
`src/admin_errors/retention.py`

`_evict_status` (retention.py:66-79) selects `[:remaining]` ids in a single list, where `remaining = total - int(MAX_ISSUES * 0.9)` is unbounded, then hands `Issue.objects.filter(pk__in=ids)` to `_delete_in_chunks`. The chunking below it does not help: the oversized IN list is re-sent as bind parameters on every chunk SELECT and DELETE. Reproduced against this branch (SQLite 3.53.1, tests/settings.py): 40 000 open issues + `MAX_ISSUES=5000` -> `retention.run_cleanup()` raises `OperationalError: too many SQL variables`; a plain `Issue.objects.filter(pk__in=list(range(n))).count()` is OK at 32 766 and fails at 50 000. The threshold is 999 on SQLite < 3.32 (which E002 still allows — the check only requires JSON1) and 65 535 on PostgreSQL.

Consequences: `errors_cleanup` exits with a traceback, the Celery task fails, and the opportunistic hook records `last_error` and then skips a whole `CLEANUP_INTERVAL_SECONDS` — so the one rule that exists to stop unbounded growth (risk 18, spec §9.3 rule 6) fails in exactly the condition it is designed for. Reachable in normal operation: the default admission limiter allows 50 new issues/minute = 72 000/day, and it is certain the first time cleanup runs after `CLEANUP="off"`, after lowering `MAX_ISSUES`, or on an existing large table. It also contradicts the design statement in PLAN.md and DECISIONS (`p05-plan/retention`) that no rule materialises a large id list.

No test covers it: `test_eviction_order_ignored_then_resolved_then_open_down_to_hysteresis` evicts 3 rows of 12, well under `CHUNK_SIZE`.

**Fix:** Make `_evict_status` loop, taking at most `CHUNK_SIZE` ids per pass:

```python
def _evict_status(alias, status, remaining, *, dry_run):
    deleted_total = 0
    while remaining > 0:
        ids = list(
            Issue.objects.using(alias)
            .filter(status=status)
            .order_by("last_seen", "pk")
            .values_list("pk", flat=True)[: min(remaining, CHUNK_SIZE)]
        )
        if not ids:
            break
        _, per_model = Issue.objects.using(alias).filter(pk__in=ids).delete()
        deleted_total += per_model.get(Issue._meta.label, 0)
        remaining -= len(ids)
    return deleted_total
```

Add a regression test that evicts more than `CHUNK_SIZE` issues (e.g. 2500 issues with `MAX_ISSUES=100`) and asserts the surviving count is `int(MAX_ISSUES * 0.9)`.

## [MINOR] Full `VACUUM` is never tested with `PRAGMA auto_vacuum=2`, though the acceptance criterion names both values
`tests/test_retention.py`

The criterion is "`errors_cleanup --vacuum` succeeds on SQLite with `PRAGMA auto_vacuum` both 0 and 2". `test_full_vacuum_rewrites_and_database_still_usable` and `test_errors_cleanup_vacuum_warns_and_succeeds` both use `sqlite_alias()` with no `auto_vacuum` argument, i.e. the default 0. `auto_vacuum=2` is only exercised on the *incremental* path. The (full VACUUM, auto_vacuum=2) combination — where SQLite rewrites a database whose freelist is in incremental mode — has no coverage.

**Fix:** Parametrise `test_full_vacuum_rewrites_and_database_still_usable` over `auto_vacuum in (0, 2)`, or add a second case using `sqlite_alias(auto_vacuum=2)` with `vacuum=True`.

## [MINOR] PostgreSQL size query binds the table name as a text parameter and hardcodes table names
`src/admin_errors/management/commands/errors_stats.py`

`_approximate_size` runs `SELECT pg_total_relation_size(%s)` with the table name as a bound parameter. That resolves only because Django's psycopg3 backend defaults to client-side binding (`ClientCursor`), which interpolates the name as an untyped literal that Postgres coerces to `regclass`. A host running `OPTIONS={"server_side_binding": True}` sends a genuine `text` parameter and gets `function pg_total_relation_size(text) does not exist`. The three table names are also hardcoded in `_PG_TABLES` rather than read from the models. Nothing exercises this branch — there is no test for the PostgreSQL size path, so Phase 6's PG pass will only catch it if that option is set.

**Fix:** Use an explicit cast and derive the names from the models: `cursor.execute("SELECT pg_total_relation_size(%s::regclass)", [model._meta.db_table])` over `(Issue, Event, IssueDailyCount)`.

## [MINOR] `--vacuum --dry-run` prints the exclusive-lock warning for a VACUUM that never runs
`src/admin_errors/management/commands/errors_cleanup.py`

`handle()` writes the "full VACUUM rewrites the whole database file and holds an exclusive lock" warning to stderr whenever `--vacuum` is passed, before calling `run_cleanup`. Under `--dry-run`, `run_cleanup` skips rule 7 entirely and the report prints `vacuum: none`. An operator combining the two flags (the natural thing to do before committing to a vacuum) gets a scary warning about an operation that did not happen, next to output saying it did not happen.

**Fix:** Gate the warning on `options["vacuum"] and not options["dry_run"]`, and optionally print `vacuum: skipped (dry run)` in that case.

## [MINOR] Dry-run eviction count is asserted only as `> 0`, leaving the one non-obvious formula unpinned
`tests/test_retention.py`

`test_dry_run_reports_every_rule_and_deletes_nothing` asserts `report.evicted_issues > 0`. The dry-run eviction number is the only value in `run_cleanup` computed arithmetically rather than measured (`max(0, total - would_delete - target)`, retention.py:139-145), and DECISIONS `p05-plan/retention` justifies it precisely on the grounds that a naive count would disagree with a real run. `> 0` cannot distinguish the intended value from an off-by-`would_delete` result, which is the exact mistake the formula exists to avoid.

**Fix:** Assert the exact expected number (14 rows, `would_delete` 3, `MAX_ISSUES=5`, `target=4` -> 7), and ideally add a companion test that runs the same fixture twice — once with `dry_run=True`, once for real — and asserts the two reports' `evicted_issues` agree.

## [MINOR] Dedicated-alias test does not assert `IssueDailyCount` placement
`tests/test_routers.py`

`test_capture_writes_only_to_the_dedicated_alias` checks `Issue` and `Event` on both aliases but not `IssueDailyCount`, although PLAN.md T8 and the acceptance criterion both say all three models. `IssueDailyCount` is written on a different code path in `storage.store_batch` (update-then-savepoint-create), so it is not covered by the other two assertions. `IssueDailyCount` is imported in the module but unused in this test.

**Fix:** Add `assert IssueDailyCount.objects.using("errors").count() == 1` and `assert IssueDailyCount.objects.using("default").count() == 0`.

## [NIT] W002 only recognises handlers declared with a `class` key
`src/admin_errors/checks.py`

`_logger_has_admin_errors_handler` reads `handler_config.get("class", "")`. `logging.config.dictConfig` also accepts the `'()'` custom-factory key; a host wiring `AdminErrorsHandler` through `{'()': 'admin_errors.handlers.AdminErrorsHandler'}` alongside `propagate: False` gets a spurious W002 telling them to add a handler they already have.

**Fix:** Fall back to `handler_config.get("()")` when `class` is absent, resolving it the same way (string compare plus `import_string` + `issubclass`, skipping non-string factories).
