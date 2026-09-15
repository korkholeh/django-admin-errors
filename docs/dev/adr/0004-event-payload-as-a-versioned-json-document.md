# 0004. The event payload is stored as a single versioned JSON document, not as normalised rows

- **Status:** accepted
- **Date:** 2026-09-15

## Context

One captured event carries a deeply nested, heterogeneous structure (spec §6.4): a chain of exceptions,
each with up to `MAX_FRAMES` (50) frames, each frame with pre/post context lines and a dict of local
variables; plus request method, URL, query, POST, headers, cookies, user and optional body; plus optional
Celery context; plus arbitrary `extra` from the log record; plus server metadata.

How it is actually used matters more than how it is shaped:

- It is **written once** and **never updated**.
- It is **always read whole**, to render one detail page for one event.
- It is **never queried by its contents**. Every filter, search and aggregate in the admin (§12.2) runs
  against `Issue` columns — status, level, exception_type, title, culprit, last_seen — not against
  payload internals.
- The shape **will change** as the library grows; the host database it lives in belongs to someone else.

The constraint that decides it: SQLite is first-class, so PostgreSQL `jsonb` indexing and containment
operators are unavailable anyway. Django's `JSONField` on SQLite is a `TEXT` column with JSON1
functions — perfectly good for store-and-fetch, not a query substrate we can rely on.

## Decision

Store the whole event as one JSON document in `Event.payload` (`JSONField`), with a schema version as
its first key: `{"v": 1, …}`. `Issue.last_event` holds a copy of the most recently *stored* event's
payload for the same reason — so the detail page still renders after event retention has deleted every
`Event` row for that issue.

Rules:

1. **Every value is JSON-serialisable before it leaves the capturing thread.** Variables become strings
   via a safe `repr()` that catches everything and falls back to `<unrepresentable ClassName>`.
   Serialisability is not hoped for; it is enforced at build time.
2. **`JSONField` is opaque.** No query ever filters, orders or aggregates on payload contents. It is
   fetched by primary key and rendered.
3. **Readers branch on `v` and tolerate missing keys.** `request`, `celery` and `extra` are optional by
   specification. A payload with an unknown future `v` renders as raw JSON rather than crashing the page.
4. **The document is size-capped** at `MAX_PAYLOAD_BYTES` (64 KiB), enforced by degrading in a fixed,
   documented order: drop frame `vars`, then context lines, then truncate the message.
5. **Never store settings or environment** in the payload, unlike Django's debug page (spec §16).

## Alternatives considered

- **Normalised tables — `Event`, `Frame`, `FrameVar`, `RequestHeader`, …** — rejected: 20–50 extra rows
  per event, a join-heavy read path for a page that always wants everything anyway, a schema migration
  in someone else's production database every time the payload gains a field, and a much worse
  bounded-storage story (row counts become the thing to cap, not bytes).
- **A `TextField` holding `json.dumps` output.** — rejected: `JSONField` gives the same storage on
  SQLite while handling encoding, decoding and backend differences, and leaves the door open to
  PostgreSQL-side inspection for a human debugging by hand.
- **Pickle or another binary format.** — rejected: unpickling data derived from request input is a
  remote-code-execution surface, and the column stops being readable by `psql` or the Django shell.
- **Compress the JSON (gzip into a `BinaryField`).** — rejected as premature: payloads are 5–15 KiB in
  practice and already capped at 64 KiB, and the sampling and ring-buffer bounds (ADR 0006) are what
  actually control storage. Compression would make the column unreadable by hand for a ~3× saving on a
  quantity that is already small.
- **No version key, relying on duck-typing at render time.** — rejected: the app writes into a database
  that outlives several of its own releases. One integer makes forward compatibility explicit instead of
  accidental.

## Consequences

**Buys.** Adding a payload field is a code change, not a migration in a host's production database.
The detail page is one row fetch. Storage is bounded in bytes, which is the unit that actually matters.
Old events stay renderable after upgrades.

**Costs.** No querying by payload content — "find every event where the user was X" is not possible and
is not offered. Denormalisation: `Issue.last_event` duplicates one `Event.payload`, which is a deliberate
availability trade (the detail page must render after event cleanup).

**Becomes harder.** Analytics across events. If that is ever wanted, the answer is to promote the needed
value to an `Issue` or `Event` column, not to start querying the JSON.

**Revisit when.** A feature genuinely needs to filter across events by payload content — at which point
the right move is a promoted column plus an index, not a change to this decision.
