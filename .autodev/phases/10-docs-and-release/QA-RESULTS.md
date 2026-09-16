# QA results — phase 10

## T9: migration invariant

```
$ uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings
No changes detected in app 'admin_errors'
$ echo $?
0
```

`src/admin_errors/migrations/` contains exactly `0001_initial.py` (confirmed by
`test_exactly_one_migration_ships`). No squash needed.

## T11: §13 manual QA e2e spec — SQLite

Run and recorded by session 2 (see PLAN.md T11 handoff note for the full narrative): fresh
`make e2e-down && make e2e-up` then `uv run --extra e2e pytest e2e -q` → **25 passed, 1 deselected**;
`make e2e-down` afterwards; `lsof -nP -iTCP:8000 -sTCP:LISTEN` and `.autodev/e2e-server.pid` both
confirmed nothing left listening. Re-confirmed clean by session 3's T13 run below.

## T12: PostgreSQL pass

```
$ make pg-up
 Container demo-postgres-1 Healthy

$ DJANGO_DB=postgres ADMIN_ERRORS_TEST_PG_URL=postgres://postgres:postgres@localhost:5432/admin_errors_test uv run pytest -q
362 passed, 1 warning in 14.85s
```

(362 vs. 354 on SQLite: the 8 `postgres_only`-guarded cases run instead of skipping.)

§13 script against a PostgreSQL-backed demo (`DEMO_DB=postgres`), reusing the T11 e2e spec unchanged:

```
$ DEMO_DB=postgres uv run python demo/manage.py migrate --noinput
No migrations to apply.
$ DEMO_DB=postgres uv run python demo/manage.py demo_seed --issues 40 --days 30 --reset
seeded 40 issues
$ DEMO_DB=postgres uv run python demo/manage.py runserver 127.0.0.1:8000 --noreload &   # backgrounded, logged to /tmp/admin-errors-e2e-server.log
$ uv run --extra e2e pytest e2e -q
25 passed, 1 deselected in 26.16s
$ DEMO_DB=postgres uv run python demo/manage.py errors_cleanup --dry-run
(dry run — nothing deleted)
events: 0
daily_counts: 0
resolved_issues: 0
ignored_issues: 0
open_issues: 0
evicted_issues: 0
vacuum: none
duration_seconds: 0.017
```

Teardown:

```
$ kill $(cat .autodev/e2e-server.pid) && rm -f .autodev/e2e-server.pid
$ lsof -nP -iTCP:8000 -sTCP:LISTEN
(empty)
$ make pg-down
 Container demo-postgres-1 Removed
$ docker compose -f demo/docker-compose.yml ps
NAME  IMAGE  COMMAND  SERVICE  CREATED  STATUS  PORTS
(no rows)
```

The whole §13 script passes on both backends: SQLite (session 2, T11) and PostgreSQL (this run, T12).

## T13: final gate and teardown

```
$ uv run ruff check . && uv run ruff format --check .
All checks passed!
101 files already formatted

$ uv run python -m django check --settings=tests.settings
System check identified no issues (0 silenced).

$ uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings
No changes detected in app 'admin_errors'

$ uv run pytest -q
354 passed, 8 skipped, 1 warning in 10.43s

$ uv run pytest -q --cov=admin_errors --cov-fail-under=90
Required test coverage of 90% reached. Total coverage: 91.03%
354 passed, 8 skipped, 1 warning in 12.35s

$ uv run tox -e package
package_smoke: OK — templates, static files and the uk catalogue all resolve.
  package: OK
  congratulations :)

$ make e2e-up   # fresh SQLite demo
e2e: server ready
$ uv run --extra e2e pytest e2e -q
25 passed, 1 deselected in 25.34s
$ make e2e-down
$ lsof -nP -iTCP:8000 -sTCP:LISTEN   # empty
$ ls .autodev/e2e-server.pid         # No such file or directory

$ uv run python benchmarks/bench_capture.py
row                         p50 (ms)    p95 (ms)   budget (ms)   ok?
capture, no locals             1.333       1.411         2.000   yes
capture, with locals           1.678       1.809        10.000   yes
capture, count-only            0.013       0.014         0.300   yes
store_batch throughput: 10000 occurrences / 50 aggregates in 0.056s -> 177,269 occurrences/s
exit: 0

$ lsof -nP -iTCP:8000 -sTCP:LISTEN
(empty)
$ docker compose -f demo/docker-compose.yml ps
NAME      IMAGE     COMMAND   SERVICE   CREATED   STATUS    PORTS
(no rows)
```

All gate commands green, environment proven clean (no listener on 8000, no compose service running).
`CHANGELOG.md`'s `0.1.0` section already carries the T2 benchmark numbers; nothing left to add.

## Post-review_fix1 e2e re-run (review round 2, minor #4)

review_fix1 materially rewrote `test_storm_is_fast_and_stores_few_events` (added the `_parse_compact_count`
helper and the `>= 500` count floor) and renamed
`test_resolve_then_rehit_shows_regressed_badge`, but the QA-RESULTS.md runs above predate that change.
Re-ran the full cycle against a fresh demo server to record evidence that matches the committed spec:

```
$ make e2e-down   # pre-clean; already clean, exit 1 (no server running)
$ make e2e-up     # fresh SQLite demo
e2e: server ready
$ curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/admin/login/
200
$ uv run --extra e2e pytest e2e -q
25 passed, 1 deselected in 25.37s
$ make e2e-down
$ lsof -nP -iTCP:8000 -sTCP:LISTEN   # empty
$ ls .autodev/e2e-server.pid         # No such file or directory
$ docker compose -f demo/docker-compose.yml ps
NAME      IMAGE     COMMAND   SERVICE   CREATED   STATUS    PORTS
(no rows)
```

Green, and environment proven clean again.
