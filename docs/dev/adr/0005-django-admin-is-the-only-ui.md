# 0005. The entire UI is built from Django admin templates with vanilla JS and no external assets

- **Status:** accepted
- **Date:** 2026-09-15

## Context

The target user is defined by the spec as "an operator who already lives in the Django admin" (§1). The
UI has to deliver a real amount of interface — an issue list with summary cards, badges, filters, search
and 14-day sparklines; a detail page with a collapsible traceback, per-frame locals toggles, a request
section, a 30-day chart, an events list and a copy-as-text button (§12.2, §12.3).

Constraints that narrow the field hard:

- **Zero runtime dependencies beyond Django** (§3). No `django-crispy-forms`, no charting package.
- **Air-gapped deployments are a target environment** (§1). Nothing may be fetched from a CDN at render
  time — not a font, not a chart library, not a stylesheet.
- **It must work in a minimal host** with only `django.contrib.{admin,auth,contenttypes,sessions,messages}` (§3).
  No `humanize`, no `sites`.
- **Dark mode must work** — the Django admin has had a dark theme since 4.2 and operators use it.
- **No build step.** The package is a wheel; a JS toolchain in the release pipeline would be a new class
  of problem for a library whose whole pitch is "just add it to `INSTALLED_APPS`".
- Assets must be CSP-friendly: no inline `<script>`.

## Decision

Register `IssueAdmin` on the admin site and build every screen as a template that **extends the admin's
own**: `change_list.html` extends `admin/change_list.html`, `change_form.html` extends
`admin/change_form.html`, plus `event_detail.html` and partial includes for traceback, request, events,
chart and cards. Custom views (event detail, status transitions) are wrapped in `site.admin_view` and
reached through `IssueAdmin.get_urls()`.

Supporting rules:

- **Styling uses only Django admin CSS variables** — `--primary`, `--body-bg`, `--body-fg`,
  `--darkened-bg`, `--hairline-color`, `--error-fg`, `--message-warning-bg`. No hard-coded colours. This
  is what makes light and dark mode work for free, and what keeps us correct when a host themes the admin.
- **Charts are inline SVG generated server-side by template tags** from integers only — a 120×24
  sparkline on the list, a 30-day bar chart on the detail page. No charting library, no canvas, no JS.
- **JavaScript is vanilla and optional**: `admin_errors.js` ≤ 3 KiB, loaded as a static file (never
  inline), handling only the frame-collapse toggles, the locals toggles and the copy button. Every one of
  those is an enhancement — the traceback, the locals and the copyable text are all present in the HTML
  without it.
- **Budgets**: `admin_errors.css` ≤ 6 KiB, `admin_errors.js` ≤ 3 KiB, zero external requests.
- **`Event` and `IssueDailyCount` are not registered** as separate admin models; they are reached through
  the issue.
- **Registration is configurable**: `ADMIN_ERRORS["ADMIN_SITE"]` takes a dotted path to a custom
  `AdminSite`, or `False` to skip auto-registration entirely and let the host call
  `admin_errors.admin.register(site)` itself.
- **`has_add_permission` is always `False`** and every field is in `readonly_fields`; the status-transition
  buttons are the only mutation path. `has_change_permission` still returns `True` for `change_issue`
  holders so the change view remains reachable.
- All strings are `{% translate %}`-wrapped, with a Ukrainian catalogue shipped.

## Alternatives considered

- **A JS SPA (React/Vue) against a REST endpoint.** — rejected: a REST API is an explicit non-goal (§2),
  it needs a build step and shipped bundles, and it means re-implementing authentication and permission
  checks that the admin already enforces.
- **Standalone Django views on their own URLs, outside the admin.** — rejected: separate login, separate
  permission wiring, separate base template and navigation, and the host has to add a URL include. The
  admin gives all of that, plus the operator's existing session and muscle memory.
- **htmx for the toggles and the event switcher.** — rejected even though the run's stack profile names
  it: it would be the only runtime dependency beyond Django, it must be fetched or vendored (bad for
  air-gapped installs), and the interactions here — collapse a block, reveal locals, copy text — are
  three lines of vanilla JS each with no server round-trip involved.
- **A charting library (Chart.js, plotly) from a CDN or vendored.** — rejected: CDN breaks air-gapped
  installs and CSP; vendoring adds 60–200 KiB to the wheel. Server-rendered SVG from integers is ~30
  lines of a template tag and is also accessible and printable.
- **Rendering the traceback with Django's own `technical_500_response`.** — rejected: it renders from a
  live exception and collects settings, and it is not a supported public API.

## Consequences

**Buys.** Authentication, permissions, CSRF, pagination, search, filters, messages, breadcrumbs, dark
mode and responsive layout all come from the admin. The wheel stays tiny and has no assets to build.
Air-gapped and CSP-strict deployments work with no special handling. A host that themes its admin gets a
themed error UI for free.

**Costs.** The UI is bounded by what admin templates can express — there will be no live-updating list,
no client-side filtering, no rich interactions. We are coupled to admin template block names and CSS
variables across Django 4.2 → 6.1, so the four-version test matrix has to actually render these pages,
not just call the views. `{% extends %}` on admin templates is a semi-public API that can shift between
releases.

**Becomes harder.** Any future interactive feature. The escape hatch is that `register(site)` and the
template blocks are both overridable by a host that wants more.

**Revisit when.** Admin template compatibility across supported Django versions becomes more expensive to
maintain than owning the base template would be.
