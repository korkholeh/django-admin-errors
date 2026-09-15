"""`writer.Writer`: spec section 9.1. Every case except `test_writer_thread_stores_through_its_own_
connection` runs against an injectable fake sink, so no database is needed and no test sleeps a
fixed interval to "wait for the writer" — synchronization is always a `threading.Event` or an
explicit `flush()`.
"""

from __future__ import annotations

import threading
import time

import pytest
from django.db import OperationalError
from django.test import override_settings
from django.utils import timezone

from admin_errors import writer
from admin_errors.capture import CapturedItem

pytestmark = pytest.mark.django_db


def _item(fp: str, *, payload: object = None, ts=None) -> CapturedItem:
    return CapturedItem(
        fingerprint=fp,
        meta={
            "exception_type": "ValueError",
            "title": "boom",
            "culprit": "x",
            "level": "error",
        },
        timestamp=ts or timezone.now(),
        payload=payload,
    )


class _RecordingSink:
    def __init__(self):
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.called = threading.Event()

    def __call__(self, batch, *, using):
        with self.lock:
            self.calls.append(batch)
        self.called.set()


def test_no_thread_until_first_enqueue():
    w = writer.Writer(sink=_RecordingSink())
    try:
        assert w._thread is None
        w.enqueue(_item("fp-lazy"))
        assert w._thread is not None
        assert w._thread.is_alive()
    finally:
        w.stop()


def test_items_with_one_fingerprint_reach_the_sink_as_one_aggregate():
    sink = _RecordingSink()
    w = writer.Writer(sink=sink)
    try:
        w.enqueue(_item("fp1", payload={"v": 1}))
        w.enqueue(_item("fp1", payload=None))  # count-only
        w.enqueue(_item("fp1", payload={"v": 1}))
        w.flush()

        assert len(sink.calls) == 1
        agg = sink.calls[0]["fp1"]
        assert agg.count == 3
        assert len(agg.samples) == 2  # the count-only item contributes no sample
    finally:
        w.stop()


def test_flush_on_batch_size():
    sink = _RecordingSink()
    with override_settings(
        ADMIN_ERRORS={"TRANSPORT": "sync", "FLUSH_BATCH_SIZE": 2, "FLUSH_INTERVAL_SECONDS": 60}
    ):
        w = writer.Writer(sink=sink)
        try:
            w.enqueue(_item("a"))
            w.enqueue(_item("b"))
            assert sink.called.wait(2.0)
        finally:
            w.stop()
    assert len(sink.calls) == 1
    assert set(sink.calls[0]) == {"a", "b"}


def test_flush_on_the_interval():
    sink = _RecordingSink()
    with override_settings(
        ADMIN_ERRORS={"TRANSPORT": "sync", "FLUSH_BATCH_SIZE": 1000, "FLUSH_INTERVAL_SECONDS": 0.05}
    ):
        w = writer.Writer(sink=sink)
        try:
            w.enqueue(_item("a"))
            assert sink.called.wait(2.0)
        finally:
            w.stop()
    assert len(sink.calls) == 1


def test_explicit_flush_returns_after_the_sink_saw_the_batch():
    sink = _RecordingSink()
    with override_settings(
        ADMIN_ERRORS={"TRANSPORT": "sync", "FLUSH_BATCH_SIZE": 1000, "FLUSH_INTERVAL_SECONDS": 60}
    ):
        w = writer.Writer(sink=sink)
        try:
            w.enqueue(_item("a"))
            w.flush(timeout=2.0)
            assert len(sink.calls) == 1
        finally:
            w.stop()


def test_flush_is_a_noop_when_no_thread_was_ever_started():
    w = writer.Writer(sink=_RecordingSink())
    w.flush(timeout=0.5)  # must return immediately, not hang
    assert w._thread is None


def test_queue_overflow_drops_and_counts():
    release = threading.Event()

    def blocking_sink(batch, *, using):
        release.wait(2.0)

    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync", "QUEUE_MAXSIZE": 1}):
        w = writer.Writer(sink=blocking_sink)
        writer.stats.dropped_queue_full = 0
        try:
            w.enqueue(_item("a"))  # picked up by the thread, queue becomes free
            # Give the thread a moment to pull the first item off the queue so the next two calls
            # actually see a full (maxsize=1) queue rather than racing the drain.
            deadline = time.monotonic() + 2.0
            while w._queue is not None and not w._queue.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
            w.enqueue(_item("b"))
            w.enqueue(_item("c"))
            assert writer.stats.dropped_queue_full >= 1
        finally:
            release.set()
            w.stop()


def test_raising_sink_is_swallowed_and_the_thread_survives():
    calls = []

    def flaky_sink(batch, *, using):
        calls.append(batch)
        if len(calls) == 1:
            raise RuntimeError("db is down")

    with override_settings(
        ADMIN_ERRORS={"TRANSPORT": "sync", "FLUSH_BATCH_SIZE": 1, "FLUSH_INTERVAL_SECONDS": 60}
    ):
        w = writer.Writer(sink=flaky_sink)
        writer.stats.last_error = None
        try:
            w.enqueue(_item("a"))
            w.flush(timeout=2.0)
            assert writer.stats.last_error == "RuntimeError"

            w.enqueue(_item("b"))
            w.flush(timeout=2.0)
            assert len(calls) == 2
            assert w._thread.is_alive()
        finally:
            w.stop()


def test_operational_error_is_retried_once_then_dropped():
    calls = []

    def always_fails(batch, *, using):
        calls.append(batch)
        raise OperationalError("database is locked")

    with override_settings(
        ADMIN_ERRORS={"TRANSPORT": "sync", "FLUSH_BATCH_SIZE": 1, "FLUSH_INTERVAL_SECONDS": 60}
    ):
        w = writer.Writer(sink=always_fails)
        writer.stats.batches_dropped = 0
        try:
            w.enqueue(_item("a"))
            w.flush(timeout=2.0)
        finally:
            w.stop()

    assert len(calls) == 2  # first attempt + one retry
    assert writer.stats.batches_dropped == 1
    assert writer.stats.last_error == "OperationalError"


def test_flush_returns_after_the_timeout_when_the_writer_is_wedged():
    wedged = threading.Event()

    def wedged_sink(batch, *, using):
        wedged.wait(5.0)

    with override_settings(
        ADMIN_ERRORS={"TRANSPORT": "sync", "FLUSH_BATCH_SIZE": 1, "FLUSH_INTERVAL_SECONDS": 60}
    ):
        w = writer.Writer(sink=wedged_sink)
        try:
            w.enqueue(_item("a"))
            started = time.monotonic()
            w.flush(timeout=0.2)
            elapsed = time.monotonic() - started
            assert elapsed < 2.0
        finally:
            wedged.set()
            w.stop()


def test_pid_change_restarts_the_thread(monkeypatch):
    sink = _RecordingSink()
    w = writer.Writer(sink=sink)
    try:
        w.enqueue(_item("a"))
        w.flush(timeout=2.0)
        first_thread = w._thread

        monkeypatch.setattr(writer.os, "getpid", lambda: first_thread.ident + 999999)
        w.enqueue(_item("b"))
        assert w._thread is not first_thread
        w.flush(timeout=2.0)
    finally:
        w.stop()


def test_atexit_flush_is_registered_once(monkeypatch):
    """Registration must not double up across the one path that can trigger it twice: a pid
    change restarting the thread (a same-pid re-`enqueue` never reaches `_register_atexit_once`
    at all, since `_ensure_started` returns early while the thread is alive)."""
    calls = []
    monkeypatch.setattr(writer, "_atexit_registered", False)
    monkeypatch.setattr(writer.atexit, "register", lambda fn: calls.append(fn))

    w = writer.Writer(sink=_RecordingSink())
    try:
        w.enqueue(_item("a"))
        w.flush(timeout=2.0)
        first_thread = w._thread
        assert len(calls) == 1

        monkeypatch.setattr(writer.os, "getpid", lambda: first_thread.ident + 999999)
        w.enqueue(_item("b"))
        assert w._thread is not first_thread
        w.flush(timeout=2.0)

        assert len(calls) == 1
    finally:
        w.stop()


def test_samples_capped_at_events_per_issue():
    sink = _RecordingSink()
    with override_settings(ADMIN_ERRORS={"TRANSPORT": "sync", "EVENTS_PER_ISSUE": 3}):
        w = writer.Writer(sink=sink)
        try:
            for i in range(5):
                w.enqueue(_item("fp-cap", payload={"v": 1, "i": i}))
            w.flush(timeout=2.0)
        finally:
            w.stop()

    agg = sink.calls[0]["fp-cap"]
    assert agg.count == 5
    assert len(agg.samples) == 3


def test_idle_connection_is_closed_after_the_idle_window(monkeypatch):
    closed: list[str] = []

    class _FakeConnection:
        def close(self):
            closed.append("default")

    class _FakeConnections(dict):
        def __getitem__(self, alias):
            return _FakeConnection()

    monkeypatch.setattr(writer, "IDLE_CONNECTION_SECONDS", 0.05)
    monkeypatch.setattr(writer, "connections", _FakeConnections())

    sink = _RecordingSink()
    with override_settings(
        ADMIN_ERRORS={"TRANSPORT": "sync", "FLUSH_BATCH_SIZE": 1000, "FLUSH_INTERVAL_SECONDS": 60}
    ):
        w = writer.Writer(sink=sink)
        try:
            w.enqueue(_item("a"))
            w.flush(timeout=2.0)
            assert len(sink.calls) == 1

            deadline = time.monotonic() + 2.0
            while not closed and time.monotonic() < deadline:
                time.sleep(0.02)
            assert closed == ["default"]
        finally:
            w.stop()


def test_idle_connection_close_failure_does_not_busy_spin(monkeypatch):
    class _BoomConnection:
        def close(self):
            raise KeyError("no such alias")

    class _FakeConnections(dict):
        def __getitem__(self, alias):
            return _BoomConnection()

    monkeypatch.setattr(writer, "IDLE_CONNECTION_SECONDS", 0.02)
    monkeypatch.setattr(writer, "connections", _FakeConnections())

    sink = _RecordingSink()
    with override_settings(
        ADMIN_ERRORS={"TRANSPORT": "sync", "FLUSH_BATCH_SIZE": 1000, "FLUSH_INTERVAL_SECONDS": 60}
    ):
        w = writer.Writer(sink=sink)
        writer.stats.batches_dropped = 0
        try:
            w.enqueue(_item("a"))
            w.flush(timeout=2.0)
            assert len(sink.calls) == 1

            # Several idle windows' worth of a permanently failing close must not turn into a
            # busy-spin: batches_dropped (wrong counter for this path anyway) must stay small,
            # not climb into the hundreds of thousands.
            time.sleep(0.4)
            assert writer.stats.batches_dropped <= 3
            assert writer.stats.last_error == "KeyError"
        finally:
            w.stop()


@override_settings(USE_TZ=False)
def test_aggregate_item_date_matches_the_sync_paths_utc_expression():
    import datetime

    # A naive local timestamp; near midnight so a local-vs-UTC divergence would actually bucket
    # it on a different day (spec §9.2 step 6: dates are UTC, matching `capture._dispatch`'s
    # `timestamp.astimezone(datetime.timezone.utc).date()`).
    ts = datetime.datetime(2026, 9, 15, 1, 0, 0)

    aggregates: dict = {}
    writer._aggregate_item(aggregates, _item("fp-date", payload={"v": 1}, ts=ts))
    thread_date = next(iter(aggregates["fp-date"].dates))

    assert thread_date == ts.astimezone(datetime.timezone.utc).date()


def test_restart_after_fork_rebinds_a_lock_held_at_fork_time():
    w = writer.Writer(sink=_RecordingSink())
    w.enqueue(_item("a"))
    w.flush(timeout=2.0)
    try:
        # Simulate a fork landing while `self._lock` was held by another thread (e.g. a
        # concurrent `_ensure_started()`): the lock is acquired and never released, exactly as
        # it would be left in a forked child.
        stuck_lock = w._lock
        stuck_lock.acquire()

        w._restart_after_fork()

        assert w._lock is not stuck_lock
        # The child's first enqueue must not hang forever on the stale lock.
        done = threading.Event()

        def _enqueue():
            w.enqueue(_item("b"))
            done.set()

        t = threading.Thread(target=_enqueue)
        t.start()
        t.join(2.0)
        assert done.is_set()
    finally:
        w.stop()


def test_stop_leaves_thread_and_queue_when_join_times_out():
    entered = threading.Event()
    wedged = threading.Event()

    def wedged_sink(batch, *, using):
        entered.set()
        wedged.wait(5.0)

    with override_settings(
        ADMIN_ERRORS={"TRANSPORT": "sync", "FLUSH_BATCH_SIZE": 1, "FLUSH_INTERVAL_SECONDS": 60}
    ):
        w = writer.Writer(sink=wedged_sink)
        try:
            w.enqueue(_item("a"))
            assert entered.wait(2.0)
            w.stop(timeout=0.2)

            # The join timed out while the thread was still inside the sink: the references must
            # survive so `_run`'s next iteration does not hit `assert self._queue is not None`.
            assert w._thread is not None
            assert w._thread.is_alive()
            assert w._queue is not None
        finally:
            wedged.set()
            if w._thread is not None:
                w._thread.join(5.0)


@pytest.mark.django_db(transaction=True)
def test_writer_thread_stores_through_its_own_connection():
    from django.db import transaction as db_transaction

    from admin_errors.models import Issue

    with override_settings(ADMIN_ERRORS={"TRANSPORT": "thread"}):
        w = writer.get_writer()
        try:
            w.enqueue(_item("fp-own-connection", payload={"v": 1, "message": "boom"}))
            # This block's `atomic()` rolls back in the *test* thread's connection; the writer
            # thread stores through a separate connection and must be unaffected.
            try:
                with db_transaction.atomic():
                    raise RuntimeError("rolled back on purpose")
            except RuntimeError:
                pass
            w.flush(timeout=5.0)
        finally:
            w.stop()

    assert Issue.objects.filter(fingerprint="fp-own-connection").exists()
