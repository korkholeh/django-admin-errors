"""The background writer thread (spec section 9.1): the only place that runs `storage.store_batch`
under `TRANSPORT="thread"`.

Started lazily on the first `enqueue()` — never at import time, never from `AppConfig.ready()` (see
CLAUDE.md's pitfall list: the autoreloader and gunicorn `--preload` both fork/exec after `ready()`
without an intervening `enqueue()`, so an eager thread would either never run or run in the wrong
process). `os.getpid()` is compared on every `enqueue()` so a fork always gets a fresh queue and
thread instead of a silently stuck inherited one (ARCHITECTURE.md trust boundary 6 / risk 11).
"""

from __future__ import annotations

import atexit
import dataclasses
import datetime
import os
import queue
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from django.db import OperationalError, close_old_connections, connections

from admin_errors import storage
from admin_errors.conf import settings as conf

if TYPE_CHECKING:
    from admin_errors.capture import CapturedItem

# Fixed per spec section 9.1, not settings: `conf.DEFAULTS` is the frozen 39-key surface that check
# W001 and the Phase 10 README table are tested against (see DECISIONS.md p04-plan/writer).
IDLE_CONNECTION_SECONDS = 60.0
RETRY_SLEEP_SECONDS = 0.1

_WAKE = object()

Sink = Callable[..., None]


@dataclasses.dataclass
class Stats:
    enqueued: int = 0
    dropped_queue_full: int = 0
    dropped_new_issue: int = 0
    flushes: int = 0
    batches_dropped: int = 0
    receiver_errors: int = 0
    last_error: str | None = None


# Module-global, not per-`Writer`: `capture.py` increments `dropped_new_issue` on the admission path
# without starting a thread, and the counters must survive a fork-triggered `Writer` replacement
# (see DECISIONS.md p04-plan/writer).
stats = Stats()

_writer: Writer | None = None
_writer_lock = threading.Lock()
_atexit_registered = False


class Writer:
    def __init__(self, sink: Sink | None = None) -> None:
        self._sink = sink
        self._queue: queue.Queue[object] | None = None
        self._thread: threading.Thread | None = None
        self._pid: int | None = None
        self._lock = threading.Lock()
        self._flush_requests: list[threading.Event] = []
        self._shutdown = threading.Event()

    @property
    def stats(self) -> Stats:
        return stats

    def enqueue(self, item: CapturedItem) -> None:
        if self._pid is not None and self._pid != os.getpid():
            self._restart_after_fork()
        self._ensure_started()
        try:
            self._queue.put_nowait(item)  # type: ignore[union-attr]
            stats.enqueued += 1
        except queue.Full:
            stats.dropped_queue_full += 1

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._queue = queue.Queue(maxsize=conf.QUEUE_MAXSIZE)
            self._pid = os.getpid()
            self._shutdown.clear()
            self._thread = threading.Thread(
                target=self._run, name="admin_errors-writer", daemon=True
            )
            self._thread.start()
            _register_atexit_once()

    def _restart_after_fork(self) -> None:
        # Rebind before touching anything: `self._lock` may have been held by another thread at
        # the moment of `os.fork()`, in which case it stays locked forever in the child and
        # `with self._lock:` here would hang the child's first `enqueue()` permanently.
        self._lock = threading.Lock()
        self._flush_requests = []
        self._shutdown = threading.Event()
        self._queue = None
        self._thread = None
        # The old thread does not exist in the child process; any queued items are lost, which
        # ARCHITECTURE.md's failure table accepts for the fork case.

    def _run(self) -> None:
        from admin_errors import capture

        capture.mark_thread_internal()

        aggregates: dict[str, storage.Aggregate] = {}
        items_in_batch = 0
        batch_started: float | None = None
        last_activity = time.monotonic()

        while True:
            assert self._queue is not None
            timeout = self._time_to_next_flush(batch_started, last_activity)
            try:
                item: object | None = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None

            # A raise anywhere below (a bad setting turning `EVENTS_PER_ISSUE` comparisons into a
            # `TypeError`, a broken `connections[...].close()`) must not kill the thread and
            # silently drop everything accumulated so far — count it and keep looping. `_flush`
            # already has its own narrower handling for sink failures.
            pending_requests: list[threading.Event] = []
            try:
                woke = item is _WAKE
                if item is not None and not woke:
                    _aggregate_item(aggregates, item)  # type: ignore[arg-type]
                    items_in_batch += 1
                    if batch_started is None:
                        batch_started = time.monotonic()

                # A flush request is only paired with a `_WAKE` put onto this same (FIFO) queue,
                # so every item enqueued before `flush()` was called is guaranteed to have already
                # been aggregated by the time `_WAKE` is dequeued. Grabbing `_flush_requests` on
                # every loop iteration instead (rather than gating on `woke`/a size-or-interval
                # flush) would let a request appended concurrently short-circuit a batch that is
                # still being drained.
                provisional_flush = bool(aggregates) and (
                    items_in_batch >= conf.FLUSH_BATCH_SIZE
                    or (
                        batch_started is not None
                        and time.monotonic() - batch_started >= conf.FLUSH_INTERVAL_SECONDS
                    )
                )
                if provisional_flush or woke:
                    with self._lock:
                        pending_requests = self._flush_requests
                        self._flush_requests = []

                should_flush = provisional_flush or woke

                if should_flush and aggregates:
                    batch, aggregates = aggregates, {}
                    items_in_batch = 0
                    batch_started = None
                    self._flush(batch)
                    last_activity = time.monotonic()

                if not aggregates and time.monotonic() - last_activity >= IDLE_CONNECTION_SECONDS:
                    # Advance the idle clock whether or not the close succeeds: a repeatedly
                    # failing close must not pin `last_activity` in the past, which would make
                    # `_time_to_next_flush` return 0 forever and busy-spin the loop.
                    try:
                        connections[conf.DATABASE].close()
                    except Exception as exc:
                        stats.last_error = type(exc).__name__
                    finally:
                        last_activity = time.monotonic()
            except Exception as exc:
                stats.batches_dropped += 1
                stats.last_error = type(exc).__name__
                aggregates = {}
                items_in_batch = 0
                batch_started = None
            finally:
                for event in pending_requests:
                    event.set()

            if self._shutdown.is_set() and not aggregates:
                break

    def _time_to_next_flush(
        self, batch_started: float | None, last_activity: float
    ) -> float | None:
        if batch_started is not None:
            remaining = conf.FLUSH_INTERVAL_SECONDS - (time.monotonic() - batch_started)
            return max(remaining, 0.0)
        # No batch pending: block only until the idle-close deadline, so `_run` wakes up to
        # evaluate it instead of blocking forever (`queue.get(timeout=None)`), which left
        # `connections[...].close()` below unreachable on the idle path it exists for.
        remaining = IDLE_CONNECTION_SECONDS - (time.monotonic() - last_activity)
        return max(remaining, 0.0)

    def _flush(self, batch: dict[str, storage.Aggregate]) -> None:
        sink = self._sink or storage.store_batch
        close_old_connections()
        try:
            sink(batch, using=conf.DATABASE)
        except OperationalError:
            time.sleep(RETRY_SLEEP_SECONDS)
            close_old_connections()
            try:
                sink(batch, using=conf.DATABASE)
            except Exception as exc2:
                stats.batches_dropped += 1
                stats.last_error = type(exc2).__name__
                return
        except Exception as exc:
            stats.batches_dropped += 1
            stats.last_error = type(exc).__name__
            return
        stats.flushes += 1

    def flush(self, timeout: float = 2.0) -> None:
        if self._thread is None or not self._thread.is_alive():
            return
        event = threading.Event()
        with self._lock:
            self._flush_requests.append(event)
        try:
            if self._queue is not None:
                self._queue.put_nowait(_WAKE)
        except queue.Full:
            pass
        event.wait(timeout)

    def stop(self, timeout: float = 2.0) -> None:
        if self._thread is None:
            return
        self.flush(timeout)
        self._shutdown.set()
        try:
            if self._queue is not None:
                self._queue.put_nowait(_WAKE)
        except queue.Full:
            pass
        self._thread.join(timeout)
        with self._lock:
            # A timed-out join means `_run` is still executing (a wedged sink past `_flush`'s own
            # timeout budget). Clearing the references here would make its next iteration hit
            # `assert self._queue is not None`; leave them in place and let `_ensure_started()`
            # replace them once the thread actually exits.
            if not self._thread.is_alive():
                self._thread = None
                self._queue = None


def _aggregate_item(aggregates: dict[str, storage.Aggregate], item: CapturedItem) -> None:
    # UTC, matching the inline `capture._dispatch` path and storage.Aggregate's contract (spec
    # §9.2 step 6) — under `USE_TZ=False`, `item.timestamp.date()` would bucket local time instead.
    date = item.timestamp.astimezone(datetime.timezone.utc).date()
    existing = aggregates.get(item.fingerprint)
    if existing is None:
        samples = [(item.timestamp, item.payload)] if item.payload is not None else []
        aggregates[item.fingerprint] = storage.Aggregate(
            meta=item.meta,
            count=1,
            first_ts=item.timestamp,
            last_ts=item.timestamp,
            samples=samples,
            dates={date: 1},
        )
        return

    existing.count += 1
    if item.payload is not None and len(existing.samples) < conf.EVENTS_PER_ISSUE:
        existing.samples.append((item.timestamp, item.payload))
    existing.first_ts = min(existing.first_ts, item.timestamp)
    existing.last_ts = max(existing.last_ts, item.timestamp)
    existing.dates[date] = existing.dates.get(date, 0) + 1


def _register_atexit_once() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    _atexit_registered = True
    atexit.register(_atexit_flush)


def _atexit_flush() -> None:
    if _writer is not None:
        _writer.stop()


def get_writer() -> Writer:
    global _writer
    with _writer_lock:
        if _writer is None:
            _writer = Writer()
        return _writer


def reset_for_tests() -> None:
    """Stop any running writer thread and reset module-global state between tests."""
    global _writer, stats
    with _writer_lock:
        if _writer is not None:
            _writer.stop()
        _writer = None
    stats = Stats()
