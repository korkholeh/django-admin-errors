# 0002. All database writes happen in a per-process background writer thread that aggregates by fingerprint

- **Status:** accepted
- **Date:** 2026-09-15

## Context

Capture happens on a request that is already failing. Spec pillar 2 is "never slows the request path,
never raises". The dangerous cases are concrete:

- The request's transaction may already be **aborted** (a PostgreSQL `IntegrityError` earlier in the
  view poisons every subsequent query on that connection), so a write on the request's connection would
  itself fail.
- The request may be inside `ATOMIC_REQUESTS`, in which case a write on that connection would be **rolled
  back together with the failing request** — the error record would vanish exactly when it is needed.
- On SQLite the write could block on `database is locked` for the host's `busy_timeout`, turning a fast
  500 into a slow one.
- An error loop produces thousands of *identical* occurrences. Writing each one is pure waste: the data
  model only needs `count += n`.

Counter-force: serialising the traceback cannot be deferred. Frames and locals must be read while they
are alive and unchanged (spec §16). So the split cannot be "do nothing synchronously".

## Decision

Split the pipeline at a bounded in-process queue.

**Synchronously, on the capturing thread** (budget ≤ 2 ms without locals, ≤ 10 ms with): guards,
once-per-exception dedup, fingerprinting, admission and sampling decisions, payload construction,
scrubbing, `BEFORE_SEND`, size enforcement. Then a single non-blocking `put_nowait` onto
`queue.Queue(maxsize=QUEUE_MAXSIZE)`. No I/O, no lock that another thread can hold.

**Asynchronously, on one daemon thread per process** (`writer.py`): drain the queue with
`get(timeout=FLUSH_INTERVAL_SECONDS)`, aggregate into `dict[fingerprint, Aggregate]`, and flush when the
interval elapses, when `FLUSH_BATCH_SIZE` is reached, or when `flush()` is requested. The thread uses its
own database connection, so it is immune to the request's transaction state. It calls an injectable
`sink` callable (default `storage.store_batch`), which makes the thread mechanics testable without a DB.

Operational rules that make the thread safe in real deployments:

- **Lazy start on first `enqueue()`** — never at import, never in `AppConfig.ready()`. This is what makes
  the `runserver` autoreloader's two processes work.
- **Pid check on every `enqueue()`**: if `os.getpid()` differs from the pid recorded at thread start,
  discard the inherited state and start a fresh thread. This covers gunicorn `--preload`, uwsgi and
  `multiprocessing`.
- **The writer thread sets the recursion guard for its entire lifetime**, so an error raised by our own
  storage code can never be captured by us.
- **Overflow drops the newest item** and increments `stats.dropped_queue_full`. Back-pressure onto the
  host is never an option.
- **Failures are swallowed**: one retry after 100 ms on `OperationalError`, then drop the batch. Never
  re-enqueue — `Event` inserts are not idempotent, and an unbounded retry queue is how an observability
  tool takes down its host.
- **Connection hygiene**: `close_old_connections()` before each flush; close the connection after 60 s idle.
- **Shutdown**: `atexit` → `flush(timeout=2.0)`, which returns after the timeout even if the queue is not empty.

`TRANSPORT="sync"` bypasses the queue entirely and calls the sink inline. It is the default in the test
suite and the documented debugging mode.

## Alternatives considered

- **Write inline in the request path.** — rejected as the default for the four reasons above; kept as
  `TRANSPORT="sync"` because it makes the whole test suite deterministic without thread synchronisation.
- **A Celery task per error.** — rejected: makes Celery a hard dependency for the core feature, and a
  broken broker silently discards exactly the errors you most need.
- **A thread pool instead of one thread.** — rejected: aggregation by fingerprint requires a single
  owner of the aggregation dict, and the workload is one small batch per second, not a parallel one.
- **`asyncio` task instead of a thread.** — rejected: the library must work identically under WSGI,
  where there is no running loop, and under ASGI. A thread is the only shape that is the same in both.
- **An unbounded queue.** — rejected: an error storm plus a slow database would grow the queue until the
  process OOMs. A bounded queue with a counted drop is honest.
- **`os.register_at_fork` instead of a pid check.** — rejected: it is Python-version- and
  platform-sensitive, and does not cover `uwsgi`'s forking, whereas comparing a pid always works.

## Consequences

**Buys.** The request path never touches the database. Aggregation collapses an error storm into one
`UPDATE … count = count + n`, which is what makes `/storm/?n=5000` return in under a second. The writer's
own connection is immune to `ATOMIC_REQUESTS` and to aborted transactions.

**Costs.** One extra database connection per worker process — with 32 workers that is 32 connections the
host's pool must accommodate, and it is documented. Up to `FLUSH_INTERVAL_SECONDS` of latency before an
error appears in the admin. A `SIGKILL` loses whatever is queued. Process-local sampling budgets multiply
by the worker count.

**Becomes harder.** Testing: thread-transport tests need `transaction=True` and a shared-cache or
file-based SQLite test database, because `TestCase`'s wrapping transaction is invisible to other
connections (spec §16). Signal receivers for `issue_created` / `issue_regressed` run in the writer thread,
which must be documented loudly since host code will be surprised by it.

**Revisit when.** The connection cost becomes the binding constraint at high worker counts — the fix
would be an opt-in shared spool, not a different threading model.
