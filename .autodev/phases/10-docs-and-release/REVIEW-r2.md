# Review — phase 10 round 2

**Verdict:** changes_requested

Phase 10 is genuinely ship-ready and I re-verified every gate independently rather than trusting QA-RESULTS.md: `pytest -q` 354 passed/8 skipped, coverage 91.03% with `--cov-fail-under=90` exit 0, `tox -e package` exit 0 ending in `package_smoke: OK — templates, static files and the uk catalogue all resolve.`, ruff check + format clean, `makemigrations --check --dry-run` clean, exactly one migration, a 0.1.0 wheel and sdist in dist/, and a full fresh e2e cycle (`make e2e-up` → 25 passed, 1 deselected → `make e2e-down`) leaving nothing on port 8000, no pid file and no compose containers. All five round-1 findings are really fixed: the regressed-badge case is renamed with its email claim dropped and PLAN.md/plan.yaml repointed at the unit test, the storm case now has both a lower bound and a `>= 500` count floor via a compact-number parser, plan.yaml names the real search queries, the settings-default check is exact equality and the diagram slice is bounded to its own section, and CLAUDE.md documents docs/user/ and the wheel smoke. One major survives, and it is the same defect class r1 caught next door: `test_readme_faq_maps_each_silent_failure_to_an_instrument` splits on the first literal "FAQ", which lands in the Install section's "(see the FAQ below)" cross-reference, so the "FAQ section" it inspects is the entire second half of the README — I confirmed by excising the whole `## FAQ` section that the test still passes. PLAN.md maps the "README contains the FAQ items" criterion and RISKS.md row #12's closure to that test, so both proofs are hollow. Four minors: the spec-17 section check matches fragments anywhere in the body (9 of 12 sections could be deleted without failing it), a stale module docstring in e2e/test_manual_qa.py still promising a notification-email assertion, a garbled traceback-order sentence in docs/user/triage-issues.md, and no recorded e2e run after review_fix1 rewrote the e2e assertions (I ran it; it is green).

## [MAJOR] test_readme_faq_maps_each_silent_failure_to_an_instrument passes with the entire FAQ section deleted
`tests/test_docs.py`

Line 138 anchors with `text.split("FAQ", 1)[-1]`. The *first* occurrence of "FAQ" in README.md is at char 3856, inside the Install section ("(see the FAQ below for the same checks in more detail)"), so `faq_section` is everything from Install onward — the settings table, How it works, Permissions, Notifications, the whole rest of the file. Every required term (`propagate`, `DEBUG`, `CAPTURE_LEVEL`, `errors_test`, `errors_stats`, `NEW_ISSUES_PER_MINUTE`, `EVENT_SAMPLE_PER_HOUR`, `view_issue_context`) appears somewhere in that region independently of the FAQ. I proved it: excising the whole `## FAQ` section from a copy of README.md and re-running the assertion yields `missing == []` — the test still passes. PLAN.md's verification table maps the acceptance criterion "README contains the FAQ items" to this test, and RISKS.md row #12 ("capture silently disabled in the host") is closed by "the FAQ that maps every silent failure to an instrument" kept honest by this test. Both proofs are hollow. This is the same defect r1 flagged for the ASCII-diagram check, which was fixed by bounding the slice; the identical bug two functions down was not.

**Fix:** Bound the slice the same way review_fix1 bounded the diagram check: locate `re.search(r"^## FAQ$", raw_text, re.MULTILINE)`, take the text up to the next `^## ` heading, and run the required-terms check against that section only. Verify by deleting the FAQ section locally and confirming the test goes red.

## [MINOR] test_readme_has_the_spec_17_sections matches fragments anywhere in the README, so 9 of 12 sections could be deleted silently
`tests/test_docs.py`

Line 104-108 checks each of the 12 `required_fragments` against the whole lowercased README. I removed each `## ` section whose heading contains its fragment and re-checked: `install`, `settings`, `permission`, `retention`, `notification`, `celery`, `storage bound`, `faq` and `non-goal` are all still found elsewhere (settings table keys, the intro's "see *Non-goals* below", the Permissions code snippet, etc.), so deleting those nine sections outright would not fail the test. Only `screenshot`, `how it works` and `contribut` are genuinely pinned. The test names spec section 17's README outline as its contract but only enforces a quarter of it.

**Fix:** Match against the set of `^## ` / `^### ` heading lines (e.g. `headings = [h.lower() for h in re.findall(r"^#{2,3} .*$", raw_text, re.M)]`) and assert each fragment appears in at least one heading, rather than anywhere in the body.

## [MINOR] Module docstring still claims the suite covers the regression notification email
`e2e/test_manual_qa.py`

Line 3 still reads "the regressed badge with its notification email". review_fix1 correctly removed that claim from the case name, the case docstring, `e2e/plans/manual-qa.plan.yaml`'s oracle and PLAN.md's verification row — the file header was missed. A reader opening this file is told the suite asserts an email it deliberately does not assert (and structurally cannot, under the demo's shipped `NOTIFY_THROTTLE_SECONDS=3600`).

**Fix:** Change line 3 to "the regressed badge" and, matching the plan.yaml oracle, note that the email path is covered by `tests/test_notifications.py::test_regression_sends_one_email`.

## [MINOR] Traceback frame-order sentence is garbled and tells the operator nothing
`docs/user/triage-issues.md`

Line 25: "**Traceback** — innermost frame first is not how Python prints it; this page follows the same order Python's own traceback does". It reads as a half-applied edit: it opens by denying a claim it never made, then states the actual behaviour. An operator cannot tell from this whether to look at the top or the bottom of the block for the failing line — which is the one thing this bullet exists to say.

**Fix:** Replace with the positive statement only, e.g. "**Traceback** — frames in the same order Python prints them, so the line that actually raised is at the bottom. Frames from installed packages are collapsed by default (click to expand) and, with `admin_errors.view_issue_context`, each frame has a toggle for its local variables."

## [MINOR] No e2e run recorded after review_fix1 changed the e2e assertions
`.autodev/phases/10-docs-and-release/QA-RESULTS.md`

QA-RESULTS.md's T11/T12/T13 sections record e2e runs from before the fix pass. review_fix1 then materially rewrote `test_storm_is_fast_and_stores_few_events` (added the `>= 500` count floor, the `1 <=` lower bound, and the new `_parse_compact_count` helper — the latter after a real `ValueError: invalid literal for int() ... '1.2k'`) and renamed a second case, but PROGRESS.md shows only `p10-tests` (the unit suite) re-run afterwards. DECISIONS documents a targeted live probe via `manage.py shell`, not a full suite run, so the committed e2e spec had no recorded green run. I ran it: `make e2e-up` → `uv run --extra e2e pytest e2e -q` → **25 passed, 1 deselected** → `make e2e-down`, `lsof -nP -iTCP:8000 -sTCP:LISTEN` empty, no `.autodev/e2e-server.pid`, `docker compose -f demo/docker-compose.yml ps` empty. So the artefact gap, not the spec, is the defect.

**Fix:** Append a short "post-review_fix1 re-run" block to QA-RESULTS.md with the command sequence and the `25 passed, 1 deselected` output plus the teardown proof, so the recorded evidence matches the committed test file.
