# Review — phase 8 round 1

**Verdict:** changes_requested

Solid phase overall: 296 tests green on SQLite (re-run independently), full lint gate clean, no migration churn, port 8000 free after e2e, real e2e RESULTS.md with 8 admin-UI cases passing, four genuine screenshots committed, and the two hard-won product fixes (nested <form>, occurrences.html `|default:` crash) are exactly the kind of bug e2e is supposed to earn its keep on. Four findings block approval. Two are reproduced defects: (a) the Celery and Extra sections are nested inside `request.html`'s `{% if ae_payload.request %}` guard, so they never render for the non-HTTP captures they exist for — verified by rendering the include with a celery-only payload (output `'\n\n'`); (b) `admin_errors.css` uses `var(--primary)` as a text/stroke colour, which is a *background* swatch in Django's theme — 2.41:1 contrast in light and 1.72:1 in dark, visibly washing out the exception type, the Open badge and every sparkline in `docs/img/issue-list-dark.png`, against spec §12.4 and ARCHITECTURE's accessibility section, while PLAN T14 records the opposite conclusion. Two are test-integrity gaps: the filter/search tests assert presence only and cannot fail if a filter is ignored, leaving the "every filter and search field works" criterion unproven; and PLAN T12 is checked off naming a `tests/test_demo.py` test that does not exist. Everything else in the acceptance list has a test that genuinely proves it.

## [MAJOR] Celery and Extra sections never render for non-HTTP captures
`src/admin_errors/templates/admin/admin_errors/issue/includes/request.html`

The whole include is wrapped in `{% if ae_payload.request %}` (line 2), and the Celery and Extra dict tables sit inside that guard (lines 24-31). `context.build_payload` (src/admin_errors/context.py:455-470) only adds a `request` key when `build_request_block(request)` returns something — a Celery task error, a management-command error or a `capture_message()` from a script has `celery`/`extra` but no `request`. For exactly those payloads the detail page shows no task name, task id, args, kwargs or extra at all, which is the case the Celery block exists to serve. Spec §12.3 item 4 requires "Celery and Extra when present". Reproduced: rendering `includes/request.html` with `{'celery': {...}, 'extra': {...}}` and `view_issue_context` granted produced `'\n\n'`. No test catches it because every `payload_factory` fixture carries both `request` and `celery`; `test_detail_renders_events_without_a_celery_key` covers the mirror case (request, no celery) but not this one. The demo's own `/task/` probe produces this shape, so it is reachable in the shipped demo.

**Fix:** Move the Celery and Extra blocks out of `request.html` into their own include (e.g. `includes/context_blocks.html`), rendered from `change_form.html` and `event_detail.html` at the same level as `request.html`, each with its own `{% if ae_payload.celery %}` / `{% if ae_payload.extra %}` plus the `{% if perms.admin_errors.view_issue_context %}` gate. Add a test with a payload that has `celery` and `extra` and no `request` key, asserting the task name renders with `view_issue_context` and is absent without it.

## [MAJOR] `var(--primary)` used as a foreground colour fails contrast in both themes
`src/admin_errors/static/admin_errors/admin_errors.css`

Lines 6 (`.ae-issue-type`), 10 (`.ae-badge--open`), 14 (`.ae-sparkline,.ae-bar-chart{color:var(--primary)}` feeding the polyline's `stroke="currentColor"`) and 15 (`.ae-bar{fill:var(--primary)}`) use `--primary` as text/stroke colour. In Django's own stylesheets `--primary` is a background swatch for the header and breadcrumbs, never body text: `#79aec8` in light (base.css:8) and `#264b5d` in dark (dark_mode.css). Computed WCAG contrast: 2.41:1 for `#79aec8` on `#ffffff`, and 1.72:1 for `#264b5d` on `--darkened-bg` `#212121` (2.0:1 on `#121212`) — all far below 4.5:1, and below even the 3:1 non-text threshold for the sparkline. The effect is visible in the committed `docs/img/issue-list-dark.png`: the exception type (the primary identifier in every row), the Open badge and the whole Trend column read as near-invisible dark teal, while the Resolved/Ignored badges, which use `--body-fg`, are legible. Spec §12.4 requires "Must look correct in light and dark themes" and ARCHITECTURE's accessibility section requires contrast to follow the admin theme. PLAN.md T14 and DECISIONS p08-implement3/T14 record the visual check as clean ("no hard-coded colours breaking contrast. No further issues found"), so the recorded verification is wrong, not just the CSS.

**Fix:** Switch the four rules to `var(--link-fg)` (#417893 light / #81d4fa dark → 4.98:1 and 9.76:1) for the exception type and the chart stroke/fill, and give `.ae-badge--open` `--body-fg` text with a `--border-color` border like the other badges. Re-take the four screenshots and re-record the visual check. Consider adding an allowlist test asserting `--primary` appears only in `background`/`border-color` declarations, so the wrong variable cannot come back.

## [MAJOR] Filter and search tests assert presence only and cannot fail
`tests/test_admin.py`

`test_filter_by_status` (line 233), `test_filter_by_level` (243), `test_filter_by_exception_type` (253) and `test_search_matches_title_type_and_culprit` (277) each create a matching and a non-matching issue, request the changelist with the filter/query, then assert only that the *matching* issue's detail URL is in the body. A filter that is silently ignored — a renamed `parameter_name`, a `get_queryset` that drops the filter, a `search_fields` typo — returns both rows and every one of these tests still passes. `test_last_seen_filter_windows` (265) is the only one that also asserts absence, and it is correspondingly the only one that proves anything. The acceptance criterion "every filter and search field works" therefore has no failing-capable test. Separately, `test_search_matches_title_type_and_culprit` only searches by title despite its name: neither `exception_type` nor `culprit` is exercised, so dropping either from `search_fields` breaks nothing.

**Fix:** Add the paired negative assertion to all four (`assert reverse(...args=[other.pk]) not in body`), and split or extend the search test into three queries — one matching only on `title`, one only on `exception_type`, one only on `culprit` — each with the non-matching issue asserted absent.

## [MAJOR] T12 checked off naming a test that was never written
`.autodev/phases/08-admin-ui/PLAN.md`

PLAN.md T12 is marked `[x]` and names `tests/test_demo.py::test_demo_seed_creates_a_view_only_viewer` (permissions exactly `{view_issue}`, `is_staff`, not superuser, idempotent across two runs). `git diff --stat 3a7d483 -- tests/test_demo.py` is empty and `grep -rn viewer tests/` returns nothing: the test does not exist. `demo_seed._ensure_viewer()` ships with no unit coverage — the permission set, the `is_superuser=False` flag and the get_or_create idempotency are all unasserted. The only proof is `e2e/test_admin_ui.py::test_viewer_has_no_context_and_403_on_status_post`, which needs a live server and does not check the permission set or re-seeding. No DECISIONS entry justifies the omission.

**Fix:** Write the named test in `tests/test_demo.py` (it already has the `override_settings`-based demo host fixture): run `demo_seed --reset` twice, then assert the `viewer` user exists exactly once, `is_staff` is True, `is_superuser` is False, and `set(user.user_permissions.values_list('codename', flat=True)) == {'view_issue'}`. Or, if it is genuinely being deferred, uncheck T12 and log why in DECISIONS.md.

## [MINOR] `?event=` branch in change_view is unreachable, untested and 500s on bad input
`src/admin_errors/admin.py`

`change_view` (lines 259-266, 282) reads `request.GET['event']`, fetches that Event and puts it in `ae_selected_event`. Nothing references `ae_selected_event` in any template, no template ever emits an `?event=` link (occurrences.html links to the separate `admin_errors_issue_event` URL instead), and no test exercises the parameter — `grep -rn 'ae_selected_event|?event=' src/admin_errors/templates e2e tests` is empty. On top of being dead, `get_object_or_404(Event.objects..., pk='abc')` raises `ValueError` rather than `Http404`, so `?event=abc` on the change page returns a 500 that admin_errors then captures itself.

**Fix:** Either delete the branch and `ae_selected_event`, or finish it: render the selected event's payload in `change_form.html`, link to it from `occurrences.html`, coerce `event_id` with a `try: int(...) except ValueError: raise Http404`, and add a test for both the valid and the malformed id.

## [MINOR] Events table reads the unredacted payload, so only one of the two documented gates applies
`src/admin_errors/templates/admin/admin_errors/issue/includes/occurrences.html`

Line 20 renders `event.payload.request.user.username` straight off the `Event` model instances in `ae_events`, which `change_view` puts into the context without passing them through `_redact_payload`. The only protection is the template's `{% if perms.admin_errors.view_issue_context %}`. That is correct today (the view-only test passes), but it contradicts the invariant stated in `_redact_payload`'s docstring, in ADR 0007's risk-#2 mitigation, in PLAN.md ("Permission gating happens twice") and in the CHANGELOG entry ("gated twice — once in the view ... and once per template include"). Adding, say, a Celery-args column to this table later would leak with no second line of defence.

**Fix:** Build `ae_events` as a list of `(event, redacted_payload)` pairs (or attach `event.ae_payload = _redact_payload(event.payload, can_view_context)`) and have the template read only the redacted copy, so the view-level gate really does cover every payload that reaches a template.

## [MINOR] In-app frame expansion loop runs before the body exists and is a no-op
`src/admin_errors/static/admin_errors/admin_errors.js`

Lines 17-19 call `document.querySelectorAll('.ae-frame--in-app')` at parse time. Django's `Media.render_js` emits a plain `<script src=...>` with no `defer`, and the admin's `change_form.html` puts `{{ media }}` in `{% block extrahead %}`, i.e. inside `<head>` — the body is not parsed yet, so the NodeList is always empty and no `ae-frame--expanded` class is ever added. Nothing breaks (the CSS only hides `.ae-frame--library` contexts, so in-app frames are already open), but the code is dead and misleading, and the e2e `locals-toggle` / `collapsed-library-frame` cases pass on the click delegation alone.

**Fix:** Delete the loop, or move it inside a `DOMContentLoaded` handler if the intent is to also start library frames collapsed via JS. If kept, add an e2e assertion that an in-app frame actually carries `ae-frame--expanded`, otherwise the line can never be proven to work.

## [MINOR] `ADMIN_SITE=False` test does not test that registration is skipped
`tests/test_admin.py`

`test_admin_site_false_skips_registration` (line 80) asserts only `_resolve_site() is None`. The acceptance criterion is that `ADMIN_SITE=False` skips the *default registration*, i.e. that `Issue not in admin.site._registry`. Because resolution happens at module import, an `override_settings` block cannot observe that, so the test silently tests a helper instead of the contract, and a regression in the `if _site is not None: register(_site)` guard at admin.py:398-400 would go unnoticed.

**Fix:** Either add a subprocess test (`uv run python -c` with a settings module carrying `ADMIN_SITE: False`, asserting `Issue` is absent from `django.contrib.admin.site._registry` after `django.setup()` + `autodiscover()`), or extract the import-time tail into a `_autoregister()` function and call it directly under `override_settings`, asserting on a throwaway site.

## [MINOR] Bulk reopen is never tested
`tests/test_admin.py`

PLAN T9 promises `test_bulk_actions_update_rows` covering "resolve/ignore/reopen over 3 issues", but the test (line 501) only posts `action=resolve`. Bulk `ignore` is incidentally covered by `test_status_change_fires_issue_status_changed`; bulk `reopen` is covered nowhere. The acceptance criterion names all three for both bulk and single.

**Fix:** Parametrize the test over the three actions, asserting the expected terminal status for each (and, for reopen, that `resolved_at` survives, matching the single-issue behaviour tested at line 490).
