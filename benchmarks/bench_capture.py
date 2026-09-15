"""Standalone timing harness for the capture pipeline (spec section 9.1 budgets).

Not collected by pytest (`testpaths = ["tests"]`): run directly with `uv run python
benchmarks/bench_capture.py`. Exits non-zero when a budget is missed unless `--no-gate` is passed,
so it doubles as a regression check and not only as prose. The numbers a run produced belong in
`CHANGELOG.md`, not here.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

N_CAPTURES = 2000
N_OCCURRENCES = 10_000
N_AGGREGATES = 50

# (label, budget_p50_ms)
BUDGETS = {
    "capture, no locals": 2.0,
    "capture, with locals": 10.0,
    "capture, count-only": 0.3,
}


def _configure_django(db_path: str) -> None:
    import django
    from django.conf import settings

    settings.configure(
        DEBUG=False,
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": db_path},
        },
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "admin_errors",
        ],
        USE_TZ=True,
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        ADMIN_ERRORS={"TRANSPORT": "thread"},
    )
    django.setup()


def _raise_at_depth(depth: int) -> None:
    local_text = "x" * 80
    local_data = {"depth": depth, "text": local_text}  # noqa: F841 - captured via frame locals
    if depth <= 0:
        raise ValueError("benchmark boom")
    _raise_at_depth(depth - 1)


def _percentile(samples_ms: list[float], pct: float) -> float:
    ordered = sorted(samples_ms)
    index = min(int(len(ordered) * pct), len(ordered) - 1)
    return ordered[index]


def _install_noop_sink() -> None:
    from admin_errors import writer

    writer._writer = writer.Writer(sink=lambda batch, using=None: None)


def _bench_capture(*, capture_locals: bool, sample_per_hour: int | None, n: int) -> list[float]:
    """`sample_per_hour=None` means unlimited (every call builds the full payload), not "leave the
    default" — the default (10/hour) would make all but the first few of `n` calls count-only and
    silently turn the "with locals" row into a no-op measurement."""
    from django.test import override_settings

    from admin_errors import api

    overrides = {
        "CAPTURE_LOCALS": capture_locals,
        "TRANSPORT": "thread",
        "EVENT_SAMPLE_PER_HOUR": sample_per_hour,
    }

    samples_ms = []
    with override_settings(ADMIN_ERRORS=overrides):
        for _ in range(n):
            start = time.perf_counter_ns()
            try:
                _raise_at_depth(30)
            except ValueError as exc:
                api.capture_exception(exc)
            samples_ms.append((time.perf_counter_ns() - start) / 1e6)
    return samples_ms


def _bench_store_batch_throughput() -> tuple[float, float]:
    from django.utils import timezone

    from admin_errors import storage
    from admin_errors.conf import settings as conf

    now = timezone.now()
    per_aggregate = N_OCCURRENCES // N_AGGREGATES
    batch = {}
    for i in range(N_AGGREGATES):
        fp = f"bench-throughput-fp-{i}"
        batch[fp] = storage.Aggregate(
            meta={
                "exception_type": "ValueError",
                "title": "benchmark throughput",
                "culprit": "bench:0",
                "level": "error",
            },
            count=per_aggregate,
            first_ts=now,
            last_ts=now,
            samples=[(now, {"v": 1, "message": "sample"})],
            dates={now.date(): per_aggregate},
        )

    start = time.perf_counter_ns()
    storage.store_batch(batch, using=conf.DATABASE)
    elapsed_s = (time.perf_counter_ns() - start) / 1e9
    occurrences_per_s = (per_aggregate * N_AGGREGATES) / elapsed_s
    return elapsed_s, occurrences_per_s


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-gate", action="store_true", help="never exit non-zero")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "bench_capture.sqlite3")
        _configure_django(db_path)

        from django.core.management import call_command

        call_command("migrate", verbosity=0, interactive=False)

        _install_noop_sink()

        rows: list[tuple[str, float, float, float, bool]] = []

        for label, capture_locals in (
            ("capture, no locals", False),
            ("capture, with locals", True),
        ):
            samples_ms = _bench_capture(
                capture_locals=capture_locals, sample_per_hour=None, n=N_CAPTURES
            )  # unlimited: every call builds the full payload
            p50, p95 = _percentile(samples_ms, 0.50), _percentile(samples_ms, 0.95)
            rows.append((label, p50, p95, BUDGETS[label], p50 <= BUDGETS[label]))

        samples_ms = _bench_capture(capture_locals=False, sample_per_hour=0, n=N_CAPTURES)
        p50, p95 = _percentile(samples_ms, 0.50), _percentile(samples_ms, 0.95)
        budget = BUDGETS["capture, count-only"]
        rows.append(("capture, count-only", p50, p95, budget, p50 <= budget))

        from admin_errors import writer

        writer.reset_for_tests()

        elapsed_s, occurrences_per_s = _bench_store_batch_throughput()

        writer.reset_for_tests()

    print(f"{'row':<24}{'p50 (ms)':>12}{'p95 (ms)':>12}{'budget (ms)':>14}{'ok?':>6}")
    all_ok = True
    for label, p50, p95, budget, ok in rows:
        all_ok = all_ok and ok
        print(f"{label:<24}{p50:>12.3f}{p95:>12.3f}{budget:>14.3f}{'yes' if ok else 'NO':>6}")

    print()
    print(
        f"store_batch throughput: {N_OCCURRENCES} occurrences / {N_AGGREGATES} aggregates in "
        f"{elapsed_s:.3f}s -> {occurrences_per_s:,.0f} occurrences/s"
    )

    if not all_ok and not args.no_gate:
        print("\nBudget missed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
