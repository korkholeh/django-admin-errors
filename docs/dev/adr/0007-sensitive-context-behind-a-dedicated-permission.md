# 0007. Sensitive request context and frame locals are gated by a dedicated `view_issue_context` model permission

- **Status:** accepted
- **Date:** 2026-09-15

## Context

Stored payloads contain production data belonging to the host's users: POST bodies, request headers,
cookies, query strings, the authenticated user's id/username/email, Celery task arguments, arbitrary
`extra` from log calls, and — when `CAPTURE_LOCALS` is on — every local variable of every frame.

Scrubbing removes the *known* secrets. `SafeExceptionReporterFilter` handles `password`, `token`,
`Authorization`, `sessionid`, `@sensitive_variables` and `@sensitive_post_parameters`, exactly as on the
DEBUG 500 page. It does not, and cannot, remove the incidental personal data: an email address in a
local, an address in a POST body, a customer record in a repr. Under GDPR-style rules that content is
personal data, and the target deployments (data residency, air-gapped, regulated internal tools) are
precisely the ones that care.

Meanwhile the useful, low-risk part of an issue — exception type, title, culprit, traceback frames,
counts, trends, which file and line — contains almost none of that. A junior developer or an on-call
rotation should be able to see *that* without being handed production PII.

## Decision

Split reading into two permissions on the `Issue` model:

- `admin_errors.view_issue` (created automatically by Django) — the issue list, the detail page, the
  traceback with filenames, line numbers, functions and context lines, counts and trends. Also the
  request **method and path**, because a traceback without knowing which endpoint it came from is much
  less useful and a path is comparatively low-risk.
- `admin_errors.view_issue_context` (declared in `Issue.Meta.permissions`, so it is created by the same
  initial migration) — request headers, cookies, GET, POST, body, the user block, Celery task args and
  kwargs, frame **locals**, and `extra`.

Mutation stays on the standard permissions: `change_issue` for resolve/ignore/reopen, `delete_issue` for
deletion, `add_issue` unused (`has_add_permission` is always `False`).

Enforcement rules:

- Gating is at **render time**, not at storage time. Storage always keeps the scrubbed values when
  `CAPTURE_LOCALS` is enabled; what a given viewer sees depends on their permission. This keeps the
  data available to whoever is authorised without a second capture pass.
- Gating happens in **both the view and the template** (`perms.admin_errors.view_issue_context`) —
  a permission on a view is not a permission on a template include, and the plain-text "Copy as text"
  traceback must respect it too.
- Each permission is tested **in isolation**: `view_issue` only → assert headers, locals, user and Celery
  args are absent from the response body; plus `view_issue_context` → assert present; without
  `change_issue` → buttons absent *and* POST returns 403; without `delete_issue` → same. Superusers pass
  everything.
- The README documents two suggested groups — *Error viewers* (`view_issue`) and *Error responders*
  (`view_issue`, `view_issue_context`, `change_issue`).

## Alternatives considered

- **One permission for everything (`view_issue`).** — rejected: it makes "let someone see the errors"
  equivalent to "give someone production PII", which is a choice most teams would not make knowingly.
- **A settings flag instead of a permission** (`SHOW_CONTEXT_TO = "superusers"`). — rejected: it is
  global, not per-user, invisible in the admin's own permission UI, and cannot be delegated to a group.
- **Do not store sensitive context at all; `CAPTURE_LOCALS=False` by default.** — rejected: locals are
  the single most valuable thing on a traceback for debugging. The spec sets `CAPTURE_LOCALS=True`, and
  hosts that disagree already have `CAPTURE_LOCALS=False`, `CAPTURE_REQUEST_BODY=False` (already the
  default) and `BEFORE_SEND` as blunt instruments.
- **Encrypt the context and require a key to view it.** — rejected: key management in a zero-dependency
  Django app is a large new surface, and it protects against database theft — a different threat from the
  one here, which is over-broad access by legitimate staff.
- **Redact at render time with a per-field allowlist.** — rejected: a moving target that would silently
  fail for every host-specific field name. A permission boundary is coarse but honest.

## Consequences

**Buys.** "Who can see production data?" is answerable in the admin's own permission UI and delegable to
a group. Broad read access to errors becomes safe to grant. The compliance story for regulated hosts is
concrete: scrubbing plus a named permission plus retention limits.

**Costs.** Two permissions to explain and to get right. Every template that touches payload data needs a
gate, and every one of those gates needs a test — a real ongoing maintenance duty, and the most likely
place for a future leak to be introduced. Adding a new payload section in a later release means deciding
its side of the boundary, which is easy to forget.

**Becomes harder.** Sharing a screenshot of a full issue with a colleague who lacks the permission. That
is the intended friction.

**Revisit when.** Users need finer granularity (locals but not cookies), or a genuine need for
field-level redaction appears. Note that the permission is created by the initial migration, so adding a
third permission later is an ordinary additive migration, not a breaking change.
