# 0003. Occurrences are grouped by a stable sha1 fingerprint whose algorithm is a frozen public contract

- **Status:** accepted
- **Date:** 2026-09-15

## Context

Grouping is the feature. If it is too coarse, unrelated bugs merge and the operator cannot tell them
apart. If it is too fine, one bug with varying data becomes hundreds of issues, `MAX_ISSUES` eviction
starts deleting real history, and the admission limiter throws away the signal.

The fingerprint is also the `Issue`'s natural key and its only unique column. That makes the algorithm
*load-bearing on stored data*: if the algorithm changes, every existing issue's fingerprint stops
matching, and the same live bug is recorded a second time under a new row — with `count` reset,
`first_seen` wrong, and the operator's `resolved` decision silently lost.

## Decision

`sha1(":".join(parts)).hexdigest()`, stored in `Issue.fingerprint` as `CharField(40, unique=True)`.

**For exceptions**, parts are, in order:

1. Fully qualified type of the **outermost** exception (`module.QualName`).
2. `culprit` — `module.function` of the innermost **in-app** frame of that exception's traceback; if
   there is no in-app frame, the innermost frame overall.
3. The **normalised** message — `str(exc)`, first line, ≤ 200 chars, with: UUIDs → `<uuid>`; hex runs
   ≥ 8 chars → `<hex>`; `0x…` addresses → `<addr>`; ISO-8601 timestamps → `<ts>`; remaining digit runs →
   `#`; single- and double-quoted literals → `<str>`; whitespace collapsed.

**For message-only records** (no `exc_info`), parts are the logger name, `record.levelname`, and
`record.msg` — the **unformatted** template (`"Payment failed for order %s"`), not
`record.getMessage()`. The template is already the natural grouping key and needs no normalisation.

**Overrides win verbatim** (still hashed): `extra={"fingerprint": "payments-timeout"}` on a log call, or
`fingerprint=` on `capture_exception` / `capture_message`.

The algorithm is a **frozen public contract**. `test_fingerprint.py` pins golden hash values for fixed
inputs. Changing any of it is a breaking change requiring a major version bump and a documented
migration note, not a patch release.

Quoting literals is a deliberate over-merge: `KeyError: 'foo'` and `KeyError: 'bar'` become one issue.
The `culprit` part is what keeps identical messages raised from different functions apart, and the
`fingerprint=` override is the documented escape hatch — documented directly next to the rule.

## Alternatives considered

- **Hash the whole stack trace (every frame's file+line).** — rejected: one bug reached from three call
  sites becomes three issues, and *any* refactor that shifts line numbers re-creates every issue in the
  project on the next deploy.
- **Hash only the exception type.** — rejected: far too coarse. Every `ValueError` in the project
  collapses into one unusable issue.
- **Hash the raw, un-normalised message.** — rejected: `ValueError("bad value 41")` and
  `ValueError("bad value 42")` are the same bug. Without normalisation a single loop produces thousands
  of issues and evicts everything real.
- **Store grouping inputs in columns and group with a query at read time.** — rejected: the unique
  constraint is what makes the concurrent upsert safe and cheap; read-time grouping would make every
  list page an aggregation over the full event table.
- **Use the formatted message for log records.** — rejected: `"Payment failed for order 42"` and
  `"… order 43"` differ, while `record.msg` is already the exact grouping key the developer wrote.
- **A stronger hash (sha256) or a shorter one.** — sha1 is not used here for security, only for
  distribution; 40 hex chars matches the field width and is plenty. A collision would merge two issues,
  which is a cosmetic fault, not a safety one.

## Consequences

**Buys.** Stable grouping across deploys and refactors. A cheap unique key that makes the concurrent
upsert in `storage.store_batch` a one-column lookup. Deterministic, golden-value-testable behaviour.

**Costs.** The deliberate over-merge on quoted literals will occasionally hide a distinction the operator
wanted. The `culprit` computation depends on in-app frame detection, so a misconfigured
`IN_APP_INCLUDE` / `IN_APP_EXCLUDE` shifts grouping — the defaults derive from `settings.BASE_DIR`, and
the setting is documented.

**Becomes harder.** Improving grouping later. Any refinement — better normalisation, smarter culprit
selection — is a breaking change, so improvements must be batched into a major release and shipped with
a note that existing issues will be re-created.

**Revisit when.** A major version is on the table anyway, or real usage shows a normalisation rule that
merges genuinely distinct bugs.
