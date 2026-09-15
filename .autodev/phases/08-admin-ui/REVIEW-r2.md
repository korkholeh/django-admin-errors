# Review — phase 8 round 2

**Verdict:** changes_requested

Round 2 fixed every round-1 finding, verified independently: 304 tests pass on SQLite, the full lint gate is clean, the e2e cycle is 18/18 green and leaves port 8000 free, and the re-taken dark screenshot confirms the `--primary` → `--link-fg` contrast fix. The Celery/Extra include, the negative filter/search assertions, the T12 viewer test, the `?event=` deletion, the per-event `_redact_payload`, the subprocess `ADMIN_SITE=False` test and the parametrized bulk reopen are all present and real. One unjustified deviation blocks approval: ARCHITECTURE.md's Accessibility section explicitly requires `aria-expanded` on the frame-collapse buttons and SVG charts whose numbers are stated in an aria-label and reachable as text — the frame toggles carry no ARIA state, the sparkline is `role="img" aria-hidden="true"` with no textual equivalent anywhere on the changelist, and the bar chart's aria-label states no numbers. Nothing in PLAN.md or DECISIONS.md records that decision, and no test covers it. Four minors follow: the screenshot test rewrites tracked PNGs on every e2e run, the CHANGELOG claims JS-free frame collapse that the CSS makes impossible, PLAN's verification table names a deleted test, and `issue_status_changed` fires on no-op transitions.

## [MAJOR] ARCHITECTURE's accessibility contract for the two widgets this phase adds is not implemented and not waived
`src/admin_errors/templates/admin/admin_errors/issue/includes/frame.html`

.autodev/ARCHITECTURE.md's Accessibility section states three concrete rules for what this app adds on top of admin markup. Two are unmet, with no justification in PLAN.md or DECISIONS.md (grep for `aria`/`accessib` across both returns nothing) and no test.

(a) "collapsed library frames and the locals toggle are `<button>` elements with `aria-expanded`". `includes/frame.html:3` renders `<button type="button" class="ae-frame-toggle">` with no `aria-expanded`, and `static/admin_errors/admin_errors.js:11` only toggles the `ae-frame--expanded` class — it never updates any ARIA state. A screen-reader user gets an unlabelled toggle button whose expanded/collapsed state is never announced, for the exact control the rule names. (The locals toggle is fine: `<details>/<summary>` exposes `aria-expanded` implicitly.)

(b) "the sparkline and the 30-day chart are inline SVG with `role="img"` and an `<title>`/`aria-label` stating the numbers, and the same numbers are reachable as text". `admin_errors_tags.py:72,84-86` emits the sparkline as `role="img" aria-hidden="true"` with no title or label — contradictory markup, and the 14 daily counts appear nowhere else as text on the changelist, so the Trend column is simply absent for assistive tech. `_render_bar_chart` (line 112,136) sets `aria-label="per day (UTC)"`, which states no numbers; the per-bar `<rect><title>date: value</title>` elements are children of a `role="img"` root and are therefore not exposed, and the detail page has no textual daily-count equivalent either.

This is the deviation-from-ARCHITECTURE case, not a taste call: the document spells out the markup, PLAN.md T11/T14 never mention it, and DECISIONS.md records no decision to drop it.

**Fix:** In `frame.html`, add `aria-expanded="{% if frame.in_app %}true{% else %}false{% endif %}"` (and an `aria-controls` pointing at the context `<pre>` id) to `.ae-frame-toggle`; in `admin_errors.js`, set `button.setAttribute('aria-expanded', frame.classList.contains('ae-frame--expanded'))` after the class toggle. In `admin_errors_tags.py`, drop `aria-hidden="true"` from the sparkline and give both SVGs an `aria-label` that states the numbers (e.g. `_("Occurrences per day (UTC), last %(n)d days: %(values)s")`), or keep them `aria-hidden` and render the same series as visually-hidden text next to the SVG. Add a test asserting `aria-expanded` is present on every `.ae-frame-toggle` and that the rendered sparkline/chart carries the count values, and either record the choice in DECISIONS.md or amend ARCHITECTURE.md if the rule is being relaxed.

## [MINOR] The e2e acceptance command rewrites tracked binary files on every run
`e2e/test_screenshots.py`

`test_generates_light_and_dark_screenshots` is collected by the plain `uv run --extra e2e pytest e2e -q` that the acceptance criteria and CI use, and writes straight into the repo's tracked `docs/img/*.png`. Because `make e2e-up` runs `demo_seed --reset` with a fresh random seed, every e2e run produces four ~300-600 KB binary diffs in the working tree. Reproduced here: after running the acceptance cycle, `git status --short docs/img/` showed all four PNGs as modified. In CI this makes any `git diff --exit-code` cleanliness check fail, and locally it invites accidental screenshot churn in unrelated commits.

**Fix:** Mark the test with a custom marker (e.g. `@pytest.mark.screenshots`) and add `-m 'not screenshots'` to the e2e default `addopts`, so refreshing `docs/img/` is an explicit `pytest e2e -m screenshots` step; or write to a temp dir by default and only copy into `docs/img/` when an env var like `AE_WRITE_SCREENSHOTS=1` is set.

## [MINOR] CHANGELOG overclaims progressive enhancement: library-frame collapse does not work with JS disabled
`CHANGELOG.md`

The Unreleased entry says the JS "only enhances — frame collapse and the locals `<details>` toggle both work with JS disabled". `admin_errors.css:31` hides `.ae-frame--library .ae-frame-context` and `.ae-frame-locals` via CSS with no `:target`/checkbox fallback, and the only way to add `ae-frame--expanded` is the JS click handler. With JS off, a library frame's source context and locals are permanently unreachable — the opposite of the claim. The locals `<details>` half of the sentence is correct.

**Fix:** Either reword the entry to say the locals toggle is JS-free while library-frame expansion requires JS, or make the collapse JS-free (wrap each frame body in its own `<details>` like the locals block, which also gets `aria-expanded` for free and would address the accessibility finding above).

## [MINOR] Verification table cites a test that no longer exists and a test that was deliberately weakened
`.autodev/phases/08-admin-ui/PLAN.md`

The acceptance table still maps "every filter and search field works" to `test_search_matches_title_type_and_culprit`, which the round-1 fix split into `test_search_matches_title` / `test_search_matches_exception_type` / `test_search_matches_culprit` (it no longer exists — `grep -rn test_search_matches_title_type_and_culprit tests/` is empty). It also maps "`ADMIN_SITE=False` skips default registration" to `test_admin_site_false_skips_registration`, which round 1 established proves only that `_resolve_site()` returns `None`; the test that actually proves the criterion is the new `test_admin_site_false_skips_default_registration_subprocess`, which the table does not mention. Likewise T14's visual-check note still carries the pre-fix wording; the corrected re-check lives only in DECISIONS.md.

**Fix:** Update the three rows to name the tests that exist now, and append one sentence to T14 recording the post-CSS-fix re-capture and re-check (the substance is already in `.autodev/DECISIONS.md` under `p08-review_fix1`).

## [MINOR] `issue_status_changed` fires even when the status did not change
`src/admin_errors/admin.py`

`_apply_status` (admin.py:48-69) unconditionally calls `signals.send_safely(issue_status_changed, old_status=..., new_status=...)`, including when `old_status == new_status` — resolving an already-resolved issue, or bulk-resolving a selection that is already resolved, emits one "changed" event per row with identical old and new values. `test_repeated_resolve_is_idempotent` only asserts the final status, so nothing pins the signal's semantics. Phase 9 wires notifications to `issue_created`/`issue_regressed` rather than this signal, so the blast radius today is limited to host-side receivers, but a host that logs or alerts on `issue_status_changed` will see spurious events.

**Fix:** Guard the send with `if old_status != new_status:` (keeping the `UPDATE` itself unconditional so `resolved_at`/`resolved_by` are still refreshed), and add an assertion to `test_repeated_resolve_is_idempotent` that the second POST fires no signal.
