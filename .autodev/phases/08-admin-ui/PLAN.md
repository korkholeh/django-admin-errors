# Phase 8 — Admin UI

**Goal:** the operator can triage from the Django admin — find the issue, read the traceback and
context, and resolve / ignore / reopen it, with sensitive context gated behind `view_issue_context`.

## Context

**What exists.** The library is feature-complete for capture and storage (Phases 1–7): `conf.py`
(settings proxy, `ADMIN_SITE` default `None`, `EVENTS_PER_ISSUE=20`, `CAPTURE_LOCALS=True`),
`models.py` (`Issue` / `Event` / `IssueDailyCount`, `view_issue_context` permission) + the single
`0001_initial` migration, `context.py` (payload builder, spec §6.4 shape, `exception.chain` outermost
first with a per-entry `cause` flag), `capture.py`, `api.py`, `writer.py`, `storage.py` (maintains
`Issue.last_event`), `retention.py`, `signals.py` (`issue_created`, `issue_regressed` and the so-far
unused `issue_status_changed`), the three management commands, and `demo/` (13 probe URLs incl.
`/boom/`, `/boom/<n>/`, `/nested/`, `/sensitive/`; `demo_seed --issues N --days D [--reset]` creating
superuser `admin`/`admin`). 238 tests pass on SQLite, `make e2e-up` / `e2e-down` work for real,
`e2e/` has 9 browser-free + browser cases with `plans/*.plan.yaml` + a `RESULTS.md` reporter.

**What does not exist.** There is no `admin.py`, no `templates/`, no `templatetags/`, no `static/` in
the package — `admin_errors` is registered on no admin site, so the demo admin shows only auth models.
`issue_status_changed` has no sender. `docs/img/` does not exist.

**What this phase changes.** Adds the whole read/triage UI and the three status transitions; makes
`issue_status_changed` fire; extends `demo_seed` with a `view_issue`-only staff user so the permission
case is reachable from a browser; produces the four README screenshots.

**Key files (new unless marked).**

```
src/admin_errors/admin.py
src/admin_errors/templatetags/{__init__.py,admin_errors_tags.py}
src/admin_errors/templates/admin/admin_errors/issue/{change_list.html,change_form.html}
src/admin_errors/templates/admin/admin_errors/issue/event_detail.html
src/admin_errors/templates/admin/admin_errors/issue/includes/
    {cards.html,header.html,traceback.html,frame.html,request.html,dict_table.html,
     occurrences.html,chart.html,server.html}
src/admin_errors/static/admin_errors/{admin_errors.css,admin_errors.js}
tests/test_admin.py
tests/settings.py                          (modified: STATIC_URL)
tests/conftest.py                          (modified: issue/payload fixtures)
demo/demo_app/management/commands/demo_seed.py   (modified: viewer user)
e2e/plans/admin-ui.plan.yaml
e2e/test_admin_ui.py
e2e/test_screenshots.py
docs/img/issue-list-{light,dark}.png  docs/img/issue-detail-{light,dark}.png
CHANGELOG.md                               (modified)
```

## Design

**No new models, no migration.** Everything renders from existing rows: `Issue`, `Event.payload`,
`Issue.last_event` and `IssueDailyCount`. `makemigrations --check` must stay clean.

### `admin.py`

```python
class LastSeenFilter(admin.SimpleListFilter)      # 1h / 24h / 7d / 30d, parameter "seen"
class IssueAdmin(admin.ModelAdmin)
def register(site: AdminSite) -> None             # public API (ARCHITECTURE "public Python API")
def _resolve_site() -> AdminSite | None           # ADMIN_SITE resolution
```

- `list_display = ["issue_cell", "count_display", "last_seen_display", "first_seen_display",
  "status_badge", "trend"]`, `list_display_links = None` — `issue_cell` renders its own anchor to the
  change view (exception type bold, title, muted culprit) as spec §12.2 requires the whole cell to link.
- `list_filter = ["status", "level", "exception_type", LastSeenFilter]` (status/level are `choices`
  fields → 0 queries; `exception_type` → `AllValuesFieldListFilter`, 1 `DISTINCT` query),
  `search_fields = ["title", "exception_type", "culprit"]`, `ordering = ["-last_seen"]`,
  `list_per_page = 50`, no `date_hierarchy`, `actions = ["resolve", "ignore", "reopen"]`.
- `has_add_permission → False`. `has_change_permission` is left at the default (perm-based) so the
  change view stays reachable for `change_issue` holders exactly as ADR 0005 says; the form is made
  non-editable by `get_readonly_fields` returning every concrete field and by
  `render_change_form(..., show_save=False, show_save_and_continue=False,
  show_save_and_add_another=False)`. A `view_issue`-only user still reaches the detail page through
  Django's own view-permission path.
- `get_queryset` adds one filtered `Prefetch` for the sparkline:
  `Prefetch("daily_counts", queryset=IssueDailyCount.objects.filter(date__gte=today-13d)
  .order_by("date"), to_attr="recent_counts")` — the single mitigation for risk #9. The prefetch is
  applied only for the changelist (`get_queryset` is also used by the change view; harmless, one extra
  cheap query there, kept for simplicity and still inside the ≤ 15 budget).
- `changelist_view` injects the three summary-card aggregates into `extra_context["ae_cards"]`
  (unresolved issues; events in the last 24 h = sum of `IssueDailyCount.count` for today+yesterday UTC;
  issues first seen in the last 24 h) — computed only when the user has `view_issue`, which the admin
  already requires to reach the page.
- `change_view` injects `extra_context`: the payload to render (`Issue.last_event`, or the selected
  event's payload), the traceback blocks, the last `EVENTS_PER_ISSUE` events (1 query), the 30-day
  daily counts (1 query), and the four permission booleans. `templates` are set explicitly via
  `change_list_template` / `change_form_template` so the lookup does not depend on the admin's
  app/model fallback chain.

**Custom URLs** (prepended in `get_urls()`, before `super()` so the admin's `<path:object_id>/`
catch-all cannot swallow them; each wrapped in `self.admin_site.admin_view`, so `is_staff` + the admin
login redirect come from the site):

| URL | Name | Method | Permission |
|---|---|---|---|
| `<pk>/status/<action>/` (`resolve`/`ignore`/`reopen`) | `admin_errors_issue_status` | POST only (`require_POST` → 405 on GET) | `change_issue`, else `PermissionDenied` (403) |
| `<pk>/events/<event_id>/` | `admin_errors_issue_event` | GET | `view_issue` (`view_issue_context` gates the sensitive sections inside) |

Status transitions go through one helper, `_apply_status(issue, action, user)`: a single `UPDATE` via
`Issue.objects.filter(pk=...).update(...)` (idempotent — setting `resolved` twice is the same as once,
per ARCHITECTURE's failure table), setting `status`, and `resolved_at`/`resolved_by` on resolve
(`resolved_at` is deliberately *kept* on reopen so the regressed badge survives — spec §6.1). It then
fires `signals.issue_status_changed` through `signals.send_safely(issue=…, old_status=…,
new_status=…, user=…)`, so a raising receiver cannot break the admin response and is counted like any
other receiver error. The same helper backs the three bulk actions, so bulk and single share one code
path; each action also calls `self.message_user`. After a single transition: `messages.success` +
redirect to the change page.

Unlike the capture pipeline, admin code is allowed to raise (ARCHITECTURE "Error handling": a broken
admin page must be visible). The only defensive branch is the payload itself: `last_event` may be
`None` after event cleanup, and a payload may lack `exception`/`request`/`frames` (message-only
capture), so every include is wrapped in an `{% if %}` and the templates never assume a key.

**`ADMIN_SITE` resolution.** `None` → `django.contrib.admin.site`; a dotted path string →
`import_string`, accepting either an `AdminSite` instance or a class (instantiated); `False` → no
auto-registration. Resolution runs at module import (which is when `admin.autodiscover()` imports us),
guarded so a re-import cannot raise `AlreadyRegistered`.

### `templatetags/admin_errors_tags.py`

Pure functions over integers and payload dicts, no queries:

| Tag/filter | Purpose |
|---|---|
| `{% ae_sparkline issue days=14 %}` | inline SVG 120×24 polyline from `recent_counts` (falls back to `daily_counts.all()`), zero-filling missing dates |
| `{% ae_bar_chart counts days=30 %}` | inline SVG bar chart for the detail page, labelled "per day (UTC)" |
| `{% ae_status_badge issue %}` / `{% ae_level_badge level %}` | `<span class="ae-badge ae-badge--…">`; `open` + `resolved_at` → **regressed** |
| `{% ae_traceback_blocks payload as blocks %}` | reverses `exception.chain` (root cause first, like CPython) and attaches the separator for each pair: `chain[i].cause` → "The above exception was the direct cause of the following exception:", else "During handling of the above exception, another exception occurred:" |
| `ae_compact_number`, `ae_frame_label`, `ae_value` | humanized count (`1.2k`), `module.function (file:lineno)`, safe scalar rendering for dict tables |

All markup is built with `format_html` / `mark_safe` over escaped parts only (trust boundary 2:
payloads are attacker-influenced), and every user-visible string is `{% translate %}` /
`gettext_lazy`-wrapped so Phase 9's `makemessages` finds it.

### Templates

`change_list.html` extends `admin/change_list.html`, overriding `{% block content_title %}`-adjacent
`{% block result_list %}`'s surroundings only: the cards are injected above the results via
`{% block content %}`'s `{{ block.super }}` pattern, keeping search, filters, pagination and actions
from the admin. `change_form.html` extends `admin/change_form.html` and replaces `{% block field_sets %}`
with the six sections of spec §12.3 (header + buttons, traceback, request, Celery/Extra, occurrences,
server); `{% block submit_buttons_bottom %}` is emptied. `event_detail.html` extends
`admin/base_site.html` and reuses the same includes. Only admin CSS variables are used (`--primary`,
`--body-bg`, `--body-fg`, `--darkened-bg`, `--hairline-color`, `--error-fg`,
`--message-warning-bg`) — no hard-coded colours, so dark mode follows the admin.

**Permission gating happens twice** (ADR 0007, risk #2): the view puts sensitive blocks into the
context only when `request.user.has_perm("admin_errors.view_issue_context")`, *and* each include is
additionally wrapped in `{% if perms.admin_errors.view_issue_context %}`. Gated: request headers,
cookies, GET, POST, body, the user block, Celery args/kwargs, frame `vars`, `extra`. Never gated:
method, path, frame filename/lineno/function/context lines, counts, trends.

`admin_errors.js` is loaded through `IssueAdmin.Media`/`{% block extrahead %}` as a static file (no
inline script, CSP-friendly) and only *enhances*: library frames ship with a `hidden`-toggling class
whose default state is set in CSS, and the locals `<details>` element works with JS disabled.

### Test host prerequisites

`tests/settings.py` needs `STATIC_URL = "/static/"`: the admin's own `base.html` calls
`{% static %}`, which raises `ImproperlyConfigured` when `STATIC_URL` is `None`, and no test has
rendered an admin page before this phase. `django.contrib.staticfiles` stays out (the minimal-host
constraint). `tests/conftest.py` gains two fixtures used across the module: `issue_factory` (issue +
daily counts + events, with a realistic §6.4 payload including `request`, `chain`, `vars`, `celery`,
`extra`, `server`) and `payload_factory`. Sensitive values in the fixture payload are fixed constants
(`SECRET_HEADER`, `SECRET_COOKIE`, `SECRET_POST`, `SECRET_LOCAL`, `SECRET_USERNAME`) so the permission
tests can assert their *absence*.

### Deviations from ARCHITECTURE.md / spec

1. Spec §12.3's **"Copy as text" button** is a Phase 9 deliverable in the roadmap (§"Notifications,
   status transitions and i18n"); this phase leaves it out and Phase 9 adds the server-rendered
   `<pre>` plus the copy handler. The JS budget is reserved for it.
2. Spec §12.2 says the cards are "three aggregate queries"; the plan keeps three for clarity and only
   collapses them if the ≤ 12 budget turns out tight — a reduction, never an increase.
3. Template-tag unit tests live in `tests/test_admin.py` rather than a new module, keeping spec
   §14.2's module list exact.

All three are appended to DECISIONS.md under `## p08-plan`.

## Tasks

- [x] **T1: test host + fixtures.** `tests/settings.py`: add `STATIC_URL`. `tests/conftest.py`: add
  `payload_factory` and `issue_factory` fixtures (issue, N events, 30 days of daily counts, payload
  with `request`/headers/cookies/POST/user, chained `exception.chain`, frame `vars`, `celery`, `extra`,
  `server`, and the five `SECRET_*` constants). Tests: `tests/test_admin.py::test_issue_factory_builds_a_full_payload`
  (guards the fixture the whole module leans on).
- [x] **T2: `admin.py` skeleton + registration.** `IssueAdmin` with list config, `LastSeenFilter`,
  `has_add_permission = False`, all-readonly fields, `get_queryset` with the 14-day `Prefetch`,
  `register(site)`, `_resolve_site()`, import-time auto-registration. Tests:
  `test_issue_is_registered_on_the_default_site`, `test_register_on_a_custom_site`,
  `test_admin_site_false_skips_registration` (via `_resolve_site()` under `override_settings`),
  `test_admin_site_dotted_path_resolves`, `test_has_add_permission_is_false`,
  `test_all_fields_are_readonly`, `test_add_view_is_not_reachable`.
- [x] **T3: template tags.** `templatetags/admin_errors_tags.py` with the six tags/filters above.
  Tests: `test_sparkline_zero_fills_missing_days`, `test_sparkline_handles_all_zero_counts`
  (no division by zero), `test_bar_chart_renders_30_bars`, `test_status_badge_regressed`,
  `test_status_badge_resolved_and_ignored`, `test_traceback_blocks_order_and_separators`
  (cause vs. context wording, root cause first), `test_compact_number`, `test_tags_escape_payload_html`
  (a `<script>` in a title/frame is escaped).
- [x] **T4: issue list template + cards.** `change_list.html`, `includes/cards.html`,
  `changelist_view` aggregates, `admin_errors.css` first cut. Tests: `test_changelist_renders_cards`
  (the three numbers), `test_changelist_renders_badges_and_sparklines` (one `<svg` per row),
  `test_changelist_links_each_row_to_the_detail_page`, `test_changelist_empty_state`.
- [x] **T5: list query budget.** Test `test_changelist_query_budget` — 50 issues × 14 days of counts,
  `assertNumQueries(12)` as an upper bound with `assertLessEqual` semantics via
  `CaptureQueriesContext`; fails loudly if the sparkline reintroduces an N+1. Tune the implementation
  (never the bound) until it holds.
- [x] **T6: filters, search, ordering.** Tests: `test_filter_by_status`, `test_filter_by_level`,
  `test_filter_by_exception_type`, `test_last_seen_filter_windows` (1h/24h/7d/30d, frozen `now`),
  `test_search_matches_title_type_and_culprit`, `test_default_ordering_is_last_seen_desc`.
- [x] **T7: issue detail.** `change_form.html` + `includes/{header,traceback,frame,request,dict_table,
  occurrences,chart,server}.html`, `change_view` extra context. Tests:
  `test_detail_renders_traceback_frames_and_context_lines`,
  `test_detail_renders_chained_exception_separator`, `test_detail_renders_request_section`,
  `test_detail_renders_events_list_and_chart`, `test_detail_marks_in_app_and_library_frames`,
  `test_detail_renders_when_last_event_is_null`, `test_detail_renders_message_only_payload`
  (no `exception`/`frames` keys), `test_detail_query_budget` (`≤ 15`).
- [x] **T8: event detail view.** URL + `event_detail.html`. Tests: `test_event_detail_renders`,
  `test_event_detail_404_for_an_event_of_another_issue`,
  `test_event_detail_requires_view_permission` (anonymous → admin login redirect).
- [x] **T9: status transitions.** `_apply_status`, the `<pk>/status/<action>/` view, the three bulk
  actions, `issue_status_changed`. Tests: `test_single_resolve_sets_status_and_redirects`,
  `test_single_ignore`, `test_single_reopen_keeps_resolved_at`,
  `test_bulk_actions_update_rows` (resolve/ignore/reopen over 3 issues),
  `test_status_change_fires_issue_status_changed` (old/new/user payload, single *and* bulk),
  `test_status_view_rejects_get` (405), `test_unknown_action_is_404`,
  `test_repeated_resolve_is_idempotent`.
- [x] **T10: permission gating.** Tests, each with a fresh staff user holding exactly one permission
  set: `test_view_issue_only_hides_context` (asserts every `SECRET_*` constant is absent from both the
  detail and event-detail bodies, and that the page still renders — paired positive assertion),
  `test_view_issue_context_reveals_context` (same constants present),
  `test_without_change_issue_buttons_are_absent_and_post_is_403`,
  `test_without_delete_issue_delete_link_absent_and_post_is_403`,
  `test_superuser_sees_everything`, `test_actions_absent_without_change_permission`.
- [x] **T11: assets + budget.** Finish `admin_errors.css` (admin variables only) and
  `admin_errors.js` (frame collapse + locals toggle, vanilla, ES2019, no inline script). Tests:
  `test_asset_budgets` (css ≤ 6144 B, js ≤ 3072 B), `test_css_uses_no_hard_coded_colours`
  (no `#rrggbb` / `rgb(` outside `var(--…)` fallbacks), `test_no_inline_script_in_templates`
  (grep every shipped template for `<script>` without `src`).
- [x] **T12: demo seed viewer user.** `demo_seed._ensure_viewer()` → staff user `viewer`/`viewer`
  with only `view_issue`, printed in the command output. Test:
  `tests/test_demo.py::test_demo_seed_creates_a_view_only_viewer` (permissions exactly
  `{view_issue}`, `is_staff`, not superuser, idempotent across two runs).
- [x] **T13: e2e plan + browser cases.** `e2e/plans/admin-ui.plan.yaml` (oracles traced to spec §12,
  PROFILE's e2e list, this PLAN) and `e2e/test_admin_ui.py`: `issue-list-cards-and-sparkline`,
  `boom-three-hits-count-3`, `boom-n-collapse` (`/boom/1/` + `/boom/2/` collapse **with each
  other**, a distinct issue from `/boom/`'s — the p07 correction), `detail-traceback-collapsed-
  library-frame`, `locals-toggle`, `nested-chain-separator`, `resolve-then-rehit-regressed-badge`,
  `viewer-has-no-context-and-403-on-status-post`. The demo runs `TRANSPORT="thread"`, so each case
  waits for the row through a `wait_until()` reload-until-condition helper in `e2e/conftest.py`,
  never a fixed sleep. Session 3 restarted the demo server fresh (picking up session 2's
  `occurrences.html` fix) and re-ran the suite: 7/8 green, 1 red — `resolve-then-rehit-regressed-
  badge` timed out waiting for navigation after clicking "Resolve". Root-caused (not a test bug):
  `header.html`'s three status-transition `<form>`s were nested inside the admin's own outer
  `<form id="issue_form">` on the change page — invalid HTML, and Chromium's parser silently
  discards the nested `<form>` open tag and closes the *outer* form early at the first nested
  `</form>`, so the "Resolve" button actually submitted to the change-form URL, not the status URL
  (confirmed via `element.closest('form')` in a real browser: `id="issue_form"`, `action=None`).
  Fixed by dropping the nested `<form>` wrappers in favour of plain `formaction`/`formmethod`
  attributes on the buttons (valid HTML5, no nesting), with a `ae_standalone_actions` flag so
  `event_detail.html` (which has no ambient form) still gets one real wrapping `<form>`. New
  regression tests `test_change_view_has_no_nested_status_forms` /
  `test_event_detail_has_no_nested_status_forms` (`tests/test_admin.py`) walk the rendered body for
  balanced, non-nested `<form>`/`</form>` tags — this class of bug is invisible to `Client()`-based
  tests (they POST straight to the URL, never parse the HTML a real browser would) and only e2e
  catches it, matching the p08-implement2 `occurrences.html` lesson. All 8 `e2e/test_admin_ui.py`
  cases green after the fix; full `uv run pytest -q` green (296 passed, 8 skipped).
- [x] **T14: screenshots.** `e2e/test_screenshots.py` — two browser contexts
  (`color_scheme="light"` / `"dark"`), writing `docs/img/issue-list-{light,dark}.png` and
  `docs/img/issue-detail-{light,dark}.png` from the seeded demo after driving `/nested/` and
  `/sensitive/` so the detail page shows a chain and a request block. First run used the default
  1280px viewport: the admin's own `.results` wrapper is `overflow-x: auto`, and at 1280px the
  issue table (with the Status/Trend columns) is wider than that wrapper's visible area, so
  `full_page=True` (which only extends the *vertical* capture) silently clipped the Status and
  Trend/sparkline columns out of the list screenshot — exactly the columns T4's design exists to
  show. Fixed by giving the screenshot browser context a 1600×1000 viewport (measured: 1600px is
  the first width at which `.results`' `scrollWidth` no longer exceeds its `clientWidth` against
  the seeded demo data). Visual check of all four PNGs (read directly, plus cropped close-ups of
  the list table's right edge to confirm the fix): list — 3 summary cards, badges (Open/Resolved/
  Ignored/Regressed, all in the admin's own colours), full 14-day sparkline column, correct in both
  themes; detail — traceback with the chained `ValueError`→`RuntimeError` and the "direct cause"
  separator, request section with headers/cookies visibly asterisk-masked (Django's own scrubber,
  not ours), occurrences chart and table, server block; dark mode follows the admin's own palette
  throughout, no hard-coded colours breaking contrast. No further issues found. Re-checked after the
  round-1 `--primary` → `--link-fg` contrast fix (`.autodev/DECISIONS.md`, `p08-review_fix1`):
  re-ran `e2e/test_screenshots.py` against a freshly booted demo server, re-captured all four PNGs
  and visually confirmed the fix.
- [x] **T15: docs + changelog.** CHANGELOG *Unreleased* entry (admin UI, permissions, assets,
  signals); `docs/img/` committed. Re-run the full gate: `uv run pytest -q`, `make lint`,
  then the e2e cycle ending in `make e2e-down` (nothing left on port 8000).

### Session 1 handoff (implement step)

T1–T12 done: `admin.py` (registration, `IssueAdmin`, `LastSeenFilter`, custom status/event-detail
URLs, `_apply_status`, `_redact_payload` view-level gate), `templatetags/admin_errors_tags.py` (all
six tags/filters), all templates/includes, `admin_errors.css`/`.js` (well under budget: 2.9 KB /
0.5 KB), `demo_seed._ensure_viewer()`, and `tests/test_admin.py` (55 tests, all passing) covering
every T1–T11 acceptance criterion including the two `assertNumQueries`-style budgets
(`django_assert_max_num_queries`) and the permission-gating pair (secrets absent/present). Verified
this session: `uv run pytest -q` → 293 passed, 8 skipped (pre-existing postgres-only skips); `ruff
check .` + `ruff format --check .` clean; `django check` clean; `makemigrations --check --dry-run`
clean (no migration added, as required). CHANGELOG *Unreleased* was **not** yet updated — leave that
for T15 alongside the rest of that task's docs work, so the changelog entry can describe the whole
phase in one go rather than being split across sessions.

### Session 2 handoff (implement step)

Wrote `e2e/plans/admin-ui.plan.yaml` and `e2e/test_admin_ui.py` (all 8 T13 cases), `e2e/test_screenshots.py`
(T14, written but never run), and `login()`/`wait_until()` helpers in `e2e/conftest.py`. Full detail
in `.autodev/DECISIONS.md`'s `p08-implement2` section — read that before touching e2e again.

**A real product bug was found and fixed**, with a unit regression test:
`includes/occurrences.html`'s Path/task column used `event.payload.celery.task` as a filter
*argument* (`|default:`), which Django resolves without `ignore_failures` — any event without a
`"celery"` key (i.e. every plain HTTP capture) 500'd the whole issue detail page. Fixed with
`{% firstof event.payload.request.path event.payload.celery.task "—" %}`. New test:
`tests/test_admin.py::test_detail_renders_events_without_a_celery_key`. `uv run pytest -q` is green
(294 passed, 8 skipped) and the full lint gate is clean.

**Why T13 is `[~]` not `[x]`:** the fix was made *while* `make e2e-up`'s demo server was already
running. Django always wraps template loaders in `cached.Loader` regardless of `DEBUG`
(`django/template/engine.py:37-41` in the installed 5.2.17) and the server runs
`--noreload`, so the already-running process kept serving the pre-fix compiled template — re-running
`e2e/test_admin_ui.py` against that same server reproduced the identical crash byte-for-byte even
after the fix landed on disk (confirmed correct via a fresh interpreter's `get_template()`, separate
from the server process). `make e2e-down` was run before this could be re-verified, per this step's
stop-here instruction. **Next session's first move:** `make e2e-up` (fresh process) then
`uv run --extra e2e pytest e2e/test_admin_ui.py -q` — expect all 8 cases green with no further
product changes; only `detail-traceback-collapsed-library-frame`, `locals-toggle`,
`nested-chain-separator`, `resolve-then-rehit-regressed-badge` and
`viewer-has-no-context-and-403-on-status-post` were red before the stop (the other 3 were already
green). If any of those five are still red after the restart, treat it as a new bug, not a repeat of
this one — the occurrences.html fix was the only known defect. Then run `e2e/test_screenshots.py`,
look at the four generated PNGs in both themes, and record the visual check here and in
DECISIONS.md (T14) before closing with T15 (CHANGELOG *Unreleased* entry, `make e2e-down`, the
`lsof -nP -iTCP:8000 -sTCP:LISTEN` empty-output check, and the full verification table below).
General rule worth keeping for the rest of this phase and later ones: **restart the demo server
(`make e2e-down && make e2e-up`) after any template/static edit made while it's already running** —
`--noreload` only ever covered Python code changes, template caching needs the same treatment.

## Verification

```
uv run pytest -q
uv run pytest -q tests/test_admin.py -x
uv run ruff check . && uv run ruff format --check . \
  && uv run python -m django check --settings=tests.settings \
  && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings
make e2e-up && uv run --extra e2e pytest e2e -q; make e2e-down
lsof -nP -iTCP:8000 -sTCP:LISTEN     # must print nothing after e2e-down
uv run pytest -q --cov=admin_errors --cov-report=term-missing
```

| Acceptance criterion | Proof |
|---|---|
| suite green incl. `tests/test_admin.py`; list renders cards, badges, sparklines | `uv run pytest -q`; `test_changelist_renders_cards`, `test_changelist_renders_badges_and_sparklines` (T4) |
| every filter and search field works | `test_filter_by_status`, `test_filter_by_level`, `test_filter_by_exception_type`, `test_last_seen_filter_windows`, `test_search_matches_title`, `test_search_matches_exception_type`, `test_search_matches_culprit`, `test_default_ordering_is_last_seen_desc` (T6) |
| detail renders traceback, chained exceptions, request, events list, chart | `test_detail_renders_traceback_frames_and_context_lines`, `test_detail_renders_chained_exception_separator`, `test_detail_renders_request_section`, `test_detail_renders_events_list_and_chart` (T7) |
| event detail renders | `test_event_detail_renders`, `test_event_detail_404_for_an_event_of_another_issue` (T8) |
| ≤ 12 queries for a 50-issue list page, no sparkline N+1 | `test_changelist_query_budget` (T5) |
| ≤ 15 queries for the detail page | `test_detail_query_budget` (T7) |
| `view_issue` only → no header/cookie/POST/locals/user values in the body | `test_view_issue_only_hides_context` (T10) |
| `view_issue_context` makes them present | `test_view_issue_context_reveals_context` (T10) |
| no `change_issue` → buttons absent, POST 403 | `test_without_change_issue_buttons_are_absent_and_post_is_403`, `test_actions_absent_without_change_permission` (T10) |
| no `delete_issue` → same for delete | `test_without_delete_issue_delete_link_absent_and_post_is_403` (T10) |
| resolve/ignore/reopen (bulk and single) update rows and fire `issue_status_changed` | `test_single_resolve_sets_status_and_redirects`, `test_single_ignore`, `test_single_reopen_keeps_resolved_at`, `test_bulk_actions_update_rows`, `test_status_change_fires_issue_status_changed` (T9) |
| `register(custom_site)` works; `ADMIN_SITE=False` skips default registration | `test_register_on_a_custom_site`, `test_admin_site_false_skips_default_registration_subprocess` (`test_admin_site_false_skips_registration` only proves `_resolve_site()` returns `None`), `test_admin_site_dotted_path_resolves` (T2) |
| e2e cycle green, covers the listed browser cases, nothing left on port 8000 | `make e2e-up && uv run --extra e2e pytest e2e -q && make e2e-down` + `lsof` check; `e2e/test_admin_ui.py` (T13) |
| `docs/img/` has issue-list and issue-detail screenshots in light and dark; both themes checked visually | `e2e/test_screenshots.py` (T14) + the visual-check note in this PLAN and DECISIONS.md |
| css ≤ 6 KB, js ≤ 3 KB, asserted by a test | `test_asset_budgets` (T11) |
| no migration churn | `makemigrations --check --dry-run` in `make lint` |

## Risks

| Row | What this phase does |
|---|---|
| **#1 wrong product — operator cannot triage** | Closed here for the read path: the browser cases (T13) walk spec §13's manual QA script — three `/boom/` hits → count 3, collapsing, resolve → re-hit → regressed badge — instead of only asserting HTML in `Client()` tests. |
| **#2 secret/PII leak through an ungated template (render half)** | Closed here: gating in both the view and every include, and T10 asserts the five `SECRET_*` constants are *absent* for a `view_issue`-only user and present with `view_issue_context`, on both the detail and event-detail pages, each paired with a positive assertion so a broken page cannot pass. |
| **#8 admin template incompatibility 4.2 → 6.1** | Closed here: every admin test *renders* the page (`client.get` + assertions on the body), never just calls the view, so a renamed block or variable fails in the tox matrix. Only documented CSS variables; light/dark checked visually in the demo (T14). |
| **#9 N+1 in the issue list** | Closed here: one filtered `Prefetch` for 14 days, three aggregate card queries, and hard `assertNumQueries` bounds (T5, T7) tuned by changing the implementation, never the bound. |
| **#14 e2e costs more than it returns** | Held: browser cases stay at the eight flows PROFILE lists plus screenshots — no second copy of `test_admin.py`; the reload-until-visible helper replaces sleeps; `make e2e-down` runs in T15 and the `lsof` check proves the port is free. |
| **#15 scope creep** | Held: no live updates, no REST, no charts library, no extra models registered; "Copy as text", notifications and the uk catalogue stay in Phase 9. |
| **#16 checks gamed to go green** | Held: no `# pragma: no cover` in `admin.py`/tags; query bounds are fixed numbers from the spec; asset budgets are asserted, not documented. |

## Out of scope

- **"Copy as text" traceback** and its copy button → Phase 9 (roadmap deliverable there).
- **Notifications** (`notifications.py`, `NOTIFY_*`, check `W003`) → Phase 9.
- **i18n catalogue**: templates are written with `{% translate %}` here, but
  `locale/uk/LC_MESSAGES/*.po/.mo`, the `makemessages` completeness test and the
  `LANGUAGE_CODE="uk"` render test → Phase 9.
- **README UI section, FAQ, manual QA script, wheel install proof, migration squash** → Phase 10.
- **New models, bulk edit of anything but status, issue assignment, comments, saved searches,
  per-user notification preferences** → not in v0.1 (spec §2 non-goals).
