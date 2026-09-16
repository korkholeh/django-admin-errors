# Triaging issues

## The issue list

The **Issues** changelist opens on three summary cards (open issues, events in the last 24 hours,
and similar at-a-glance counts), followed by a table of issues, most recently seen first. Each row
shows:

- a **status badge** — Open, Resolved, Ignored, or Regressed (see below);
- the exception type and a short message;
- the **culprit** — the function or view where it happened;
- **count** — total occurrences seen (not just the ones stored in detail, see
  [Retention](retention.md));
- first seen / last seen;
- a small 14-day **sparkline** showing daily occurrence trend.

Use the filters (status, level, exception type, last-seen range) and the search box (matches
title, exception type, culprit) to narrow the list — for example, filter to `status = Open` and
sort by count to find your noisiest active problem.

## The issue detail page

Click an issue to open its detail page:

- **Traceback** — frames in the same order Python prints them, so the line that actually raised is
  at the bottom. Frames from installed packages are collapsed by default (click to expand) and, if
  you have `admin_errors.view_issue_context`, a toggle to show local variables for each frame.
  Chained exceptions (`raise ... from ...`) are shown with a separator between them.
  A **Copy as text** button copies a plain-text version of the traceback to your clipboard, for
  pasting into a chat message or a ticket.
- **Request** (only visible with `admin_errors.view_issue_context`) — the URL, method, headers,
  cookies, GET/POST data and the logged-in user at the time, all run through Django's own scrubbing
  so passwords, tokens and similar fields never reach this page in the first place.
- **Occurrences** — a 30-day chart and table of when this issue happened.

## Resolve, Ignore, Reopen

Three actions, available as buttons on the detail page and as bulk actions from the list
(`admin_errors.change_issue` required):

- **Resolve** — marks the issue fixed. If it happens again afterwards, its status flips to
  **Regressed** (a distinct badge from plain "Open") so a fix that didn't actually work stands out
  from a brand-new problem.
- **Ignore** — hides it from your regular triage without deleting it (e.g. known noise from a
  third-party integration). Ignored issues still count occurrences; they just don't need attention.
- **Reopen** — clears Resolved/Ignored back to Open by hand, without waiting for a new occurrence.

Resolving does not reset the notification throttle — if the same issue reappears very soon after
being marked resolved, you won't get a second email until the throttle window passes (see
[Notifications](notifications.md)).
