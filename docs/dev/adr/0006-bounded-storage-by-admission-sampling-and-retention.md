# 0006. Storage is bounded by four independent mechanisms: admission, sampling, a per-issue ring buffer, and retention with eviction

- **Status:** accepted
- **Date:** 2026-09-15

## Context

The app writes into the host's **production** database (ADR 0001) and is installed by an operator who
will not tune it and may not look at it for months. "Bounded storage" is a stated pillar (spec §1).

The failure this prevents is concrete and common: an error inside a loop, a retry storm, or a
misconfigured integration produces thousands of occurrences per second. Two distinct explosions have to
be contained separately, because they have different shapes:

- **Depth** — one bug, enormous occurrence count. Unbounded, this writes N rows of ~10 KiB each.
- **Breadth** — a bug whose message varies in a way normalisation does not catch (or a genuinely broken
  deploy), producing thousands of *distinct* fingerprints. Unbounded, this creates thousands of `Issue`
  rows and buries the real ones.

A fifth force: whatever we delete, the **operator's triage decisions** — `status`, `resolved_at`,
`resolved_by`, `notified_at` — are the one thing in this app that cannot be recreated from the host's logs.

## Decision

Four mechanisms, each cutting a different dimension, all with defaults that work untouched.

**1. Admission — bounds breadth.** A process-local bounded LRU set (10 000 entries) of seen fingerprints
plus a token bucket of `NEW_ISSUES_PER_MINUTE` (default 50). An unseen fingerprint arriving with an empty
bucket is dropped and counted in `stats.dropped_new_issue`. Already-seen fingerprints are never subject
to admission — an established issue keeps counting.

**2. Sampling — bounds depth.** A per-fingerprint token bucket of `EVENT_SAMPLE_PER_HOUR` (default 10),
process-local, in a bounded LRU of 10 000. With a token, the full payload is built and stored. Without
one, the item becomes a **count-only item**: fingerprint plus cheap meta, no payload. Count-only items
still increment `Issue.count` and `IssueDailyCount.count`, so totals stay true even though most
occurrences store nothing. Meta is always built, so an issue can be *created* from a count-only item —
necessary when retention deleted the issue while the process's bucket was already exhausted.

**3. Ring buffer — bounds per-issue depth in the database.** After inserting samples, trim to the
`EVENTS_PER_ISSUE` (default 20) newest events for that issue: list the surplus ids ordered by
`(-timestamp, -id)`, then delete by id.

**4. Retention and eviction — bounds age and total count.** `retention.run_cleanup()` runs seven ordered
rules — events older than `EVENT_RETENTION_DAYS` (30), daily counts older than
`DAILY_COUNT_RETENTION_DAYS` (90), resolved issues past `RESOLVED_ISSUE_TTL_DAYS` (14), ignored issues
past `IGNORED_ISSUE_TTL_DAYS` (`None` = keep), open issues unseen for `OPEN_ISSUE_TTL_DAYS` (90), then
eviction, then SQLite vacuum. **Eviction order is `ignored` → `resolved` → `open`, oldest `last_seen`
first, with hysteresis down to `int(MAX_ISSUES * 0.9)`** — the operator's own triage decides what is
cheapest to lose, and hysteresis stops eviction from running on every single flush at the cap. Every
delete is chunked in batches of 1 000 ids to keep SQLite write locks short.

Cleanup triggers, in decreasing order of determinism: `manage.py errors_cleanup [--vacuum] [--dry-run]`;
`admin_errors.tasks.cleanup` on a Celery beat schedule; and by default `CLEANUP="opportunistic"` — the
writer thread runs cleanup after a flush at most once per `CLEANUP_INTERVAL_SECONDS` per process, guarded
by both a process-local timestamp and `cache.add("admin_errors:cleanup-lock", …)`.

**Payload size** is capped independently at `MAX_PAYLOAD_BYTES` (64 KiB), degrading in a fixed order:
drop frame `vars`, then context lines, then truncate the message.

The resulting worst case is stated in the README: ≤ `MAX_ISSUES` issues, ≤ `MAX_ISSUES × EVENTS_PER_ISSUE`
events of ≤ 64 KiB, ≤ `MAX_ISSUES × DAILY_COUNT_RETENTION_DAYS` counter rows of ~40 bytes.

## Alternatives considered

- **Store every occurrence; let the operator run cleanup.** — rejected: the operator who most needs this
  app is the one who will never run it. An unattended install must stay bounded on its own.
- **A single global cap (`MAX_ISSUES`) and nothing else.** — rejected: it bounds breadth but not depth.
  One hot bug would still write thousands of 10 KiB event rows per minute inside a single issue.
- **Time-based retention only, with no caps.** — rejected: a bad day still writes unbounded data *during*
  that day; retention only cleans up afterwards, and the disk fills in between.
- **Shared sampling budgets via the cache backend.** — rejected: makes the error path depend on a working
  cache, which is frequently the thing that broke. Process-local budgets multiply by worker count, which
  is accepted and documented (spec §5 says "per process").
- **Reservoir sampling or exponential backoff per fingerprint** (store the 1st, 2nd, 4th, 8th …) —
  rejected: harder to reason about and to document than "N per hour", and the ring buffer already
  guarantees the stored events are the *newest*, which is what an operator debugging now actually wants.
- **Evict by occurrence count (delete the rarest issues).** — rejected: a rare issue is often the
  interesting one. Age plus operator status is a better proxy for "safe to lose".
- **Cleanup only in the writer thread, with no command.** — rejected: an operator needs a deterministic,
  inspectable path (`--dry-run`) and a cron/beat option.

## Consequences

**Buys.** A hard, documentable ceiling on database growth with zero operator configuration. A storm is
cheap: it becomes one `UPDATE … count = count + n` plus at most `EVENT_SAMPLE_PER_HOUR` inserts, which is
what makes the §18 storm criterion (`/storm/?n=5000` < 1 s, ≤ 1 issue + ≤ 5 events + 1 daily count)
achievable. The operator's triage decisions are the last thing evicted.

**Costs.** **Data is deliberately lost, and this must be documented prominently.** `Issue.count` is exact,
but stored events are a sample — an operator cannot assume the event they want was kept. During a breadth
storm, genuinely new and distinct issues are dropped entirely; `errors_stats` exposing
`dropped_new_issue` is the only way to know it happened, and the README must say so. Four interacting
limiters mean "why is my error not showing up?" has several possible answers, which is why the FAQ and
`errors_test` exist.

**Becomes harder.** Any feature needing complete event history — rate analysis, "first occurrence with
this user" — is impossible by construction.

**Revisit when.** Telemetry or user reports show the defaults are wrong for real installs, or an operator
needs a "keep everything for this one fingerprint" exemption. Note that all six numbers are settings, so
tuning is not a revisit — only changing the *mechanisms* is.
