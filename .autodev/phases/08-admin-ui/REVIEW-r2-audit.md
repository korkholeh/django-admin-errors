# Audit of the round-2 fixes — phase 8

**Verdict:** approve

All five round-2 findings are fixed in the product by this diff, each with a test that fails without it, and nothing was rejected. The MAJOR accessibility finding is fully addressed: `.ae-frame-toggle` now carries `aria-expanded` (true for in-app, false for library frames) plus `aria-controls` pointing at a unique `ae-frame-context-<block>-<frame>` id, kept in sync by `admin_errors.js` from `classList.toggle()`'s return value; both SVGs dropped `aria-hidden="true"` and now carry an `aria-label` from `_series_label()` stating every date:value pair — the first of the two options the review itself prescribed, argued in DECISIONS.md under `p08-review_fix2`. The four minors: screenshots test marked and deselected by default via registered marker + `addopts`, CHANGELOG reworded to admit library-frame collapse needs JS, PLAN verification table repointed at the tests that exist, and `issue_status_changed` guarded with `if old_status != new_status:` while the UPDATE stays unconditional. Verified independently: `uv run pytest -q` 307 passed / 8 skipped; full lint gate clean (ruff check, ruff format --check, django check, makemigrations --check); `pytest e2e` 17 passed / 1 deselected and `-m screenshots` selects exactly the screenshot test; after a full e2e cycle `git status docs/img/` shows no modification, confirming the churn fix; `make e2e-down` left port 8000 free. Rendered the nested frame include directly to confirm aria-controls/id uniqueness. No spec, ARCHITECTURE or logged-decision contradiction; the uk catalogue is phase 9's scope, so the new msgid is not a gap here.

## [MINOR] JS aria-expanded sync is not covered by any test
`src/admin_errors/static/admin_errors/admin_errors.js`

`admin_errors.js:11-12` sets `aria-expanded` after toggling `ae-frame--expanded`, but nothing asserts it: the Django test only checks the server-rendered initial attribute, and `e2e/test_admin_ui.py:118-126` clicks the toggle and asserts context visibility only. Deleting the `setAttribute` line keeps the whole suite green. Logic itself is correct — `DOMTokenList.toggle` returns the post-toggle state.

**Fix:** In `e2e/test_admin_ui.py`, after `frame.locator('.ae-frame-toggle').click()`, assert the toggle's `aria-expanded` is `"true"` (and `"false"` before the click).
