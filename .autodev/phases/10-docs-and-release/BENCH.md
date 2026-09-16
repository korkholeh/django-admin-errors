# Benchmark run (phase 10, T2)

Machine: `Darwin Mac.Dlink 25.6.0 Darwin Kernel Version 25.6.0: Fri Jul 31 19:17:26 PDT 2026;
root:xnu-12377.161.14~5/RELEASE_ARM64_T6041 arm64` (Apple Silicon Mac, macOS 26 "Tahoe" kernel).
Python `3.13.3`. SQLite `3.49.1`.

Command: `uv run python benchmarks/bench_capture.py`

```
row                         p50 (ms)    p95 (ms)   budget (ms)   ok?
capture, no locals             1.309       1.392         2.000   yes
capture, with locals           1.588       1.714        10.000   yes
capture, count-only             0.013       0.014         0.300   yes

store_batch throughput: 10000 occurrences / 50 aggregates in 0.058s -> 173,609 occurrences/s
```

Exit code: `0`. All three budgets met; no product change required. These numbers are copied into
`CHANGELOG.md`'s `[0.1.0]` section (T8) and into the README's *Storage bound and benchmarks* section
(T6).
