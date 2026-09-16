# Review — phase 10 round 1

**Verdict:** changes_requested

Phase 10 is substantively done and the gates are real: I independently re-ran the suite (354 passed / 8 skipped), the coverage gate (exit 0, 91%), `tox -e package` (exit 0, `package_smoke: OK — templates, static files and the uk catalogue all resolve.`), ruff check + format, and confirmed exactly one migration, a 0.1.0 wheel in dist/, nothing listening on port 8000, no compose containers, and no stale e2e pid file. README prose was spot-checked against code (hysteresis 0.9, queue drops newest on overflow, `admin_errors.cleanup` task name, W001–W003/E001–E002, `errors_cleanup --vacuum`) and matches; the settings table is pinned both directions against conf.DEFAULTS; docs/user/ is accurate and carries the throttle caveat phase 9 asked for; CHANGELOG 0.1.0 quotes the real BENCH.md numbers. One major issue: the acceptance criterion \"resolve then re-hit → regressed badge plus a console email\" has no test — the e2e case named ..._and_emails asserts only the badge and cannot assert the email under the demo's 3600s throttle, yet PLAN.md's verification table and the plan YAML both claim it proves the email. Four minors: a storm assertion that can pass with zero stored events, stale plan.yaml steps, substring-based default matching in tests/test_docs.py, and CLAUDE.md not mentioning docs/user/ or the new wheel smoke.

## [MAJOR] "Resolve then re-hit → regressed badge plus a console email" criterion has no test; the spec and plan claim it does
`e2e/test_manual_qa.py`

test_resolve_then_rehit_shows_regressed_and_emails (e2e/test_manual_qa.py:164) asserts only the .ae-badge--regressed element. It makes no assertion about any email, and `grep -rn "Regression" e2e/` finds nothing in the whole e2e tree. It structurally cannot assert one as written: it deliberately reuses the /boom/ issue created by the first test in the file (which already burned a 'New issue:' notification), and the demo runs the shipped NOTIFY_THROTTLE_SECONDS=3600 with resolving explicitly not resetting notified_at (accepted in DECISIONS p09-review_fix2/minor-rejected2), so the regression email is suppressed by design. Despite that, PLAN.md:309 maps the acceptance criterion "Resolve then re-hit → regressed badge + console email" to this test, e2e/plans/manual-qa.plan.yaml:17 states the file covers "the regressed badge plus a real notification email", and the test's own name ends in _and_emails. The email path itself is covered at unit level by tests/test_notifications.py:195 test_regression_sends_one_email, so the product is fine — the phase artefacts assert proof that does not exist for the end-to-end clause.

**Fix:** Either (a) make the case actually observe a 'Regression:' line — e.g. drive a culprit whose creation notification is older than the throttle window, or resolve/re-hit an issue seeded by demo_seed rather than one just created — or (b) stop claiming it: rename the case to ..._shows_regressed_badge, fix the plan.yaml oracle and PLAN.md's verification row to cite tests/test_notifications.py::test_regression_sends_one_email for the email, and log a DECISIONS entry stating that spec §13's 'and a console email' clause is unobservable under the demo's 3600s throttle.

## [MINOR] Storm case passes vacuously when no events were stored
`e2e/test_manual_qa.py`

Line 160-161 asserts `stored_rows.count() <= EVENT_SAMPLE_PER_HOUR` with no lower bound, and never reads the issue's Count column. The per-fingerprint EVENT_SAMPLE_PER_HOUR budget is process-wide for the whole demo server run, so a run in which the storm's payloads were all sampled away (or capture silently dropped them) yields count 0 and still passes, while spec §13 asks for 'issue count +5000, ≤5 stored events'. EVENT_SAMPLE_PER_HOUR = 5 is also hardcoded at line 23 against demo/demo_project/settings.py, so a demo settings change makes the bound silently wrong.

**Fix:** Assert `1 <= stored_rows.count() <= EVENT_SAMPLE_PER_HOUR` and add an assertion that the issue's Count column grew by a substantial fraction of 5000 after the storm (the README already documents that queue overflow may drop some, so a floor rather than equality).

## [MINOR] Plan steps describe searches the spec does not perform
`e2e/plans/manual-qa.plan.yaml`

Line 47 says 'search the issue list for \'bad value\'', but the test searches for `boom_n` — its own comment (e2e/test_manual_qa.py:129-131) explains that searching 'bad value' finds nothing because the title is built from the normalized message. Line 55 says 'delete any pre-existing issue for demo_app.views.storm' while the test deletes by the query 'DemoStormError'. The plan is the traceability document for these cases; a reader reproducing the steps by hand gets a different (failing) result.

**Fix:** Update the two case `steps` blocks to the queries the spec actually uses, keeping the normalization rationale from the test comment.

## [MINOR] Settings-default check matches by substring; ASCII-diagram check is near-vacuous
`tests/test_docs.py`

test_readme_settings_table_defaults_match_conf (line 87) accepts the row when `repr(expected)` is merely a substring of the documented cell, so a documented `500` satisfies an expected `50` and `36000` satisfies `3600` — exactly the stale-default drift the test exists to catch. Separately, test_readme_has_the_spec_17_sections (line 123-124) splits the README at 'How it works' and asserts '```' appears in the remainder; every later section (Install snippets, Celery beat, FAQ) contains fences, so the assertion can never fail and does not pin the pipeline diagram.

**Fix:** Compare the backticked token extracted from the Default cell for exact equality with _normalize_default(expected). For the diagram, bound the slice to the next '## ' heading and assert a fenced block containing e.g. 'writer thread' appears within it.

## [MINOR] New docs tree and wheel smoke not recorded in CLAUDE.md
`CLAUDE.md`

This phase adds docs/user/ (six operator pages) and tests/package_smoke.py, which now runs as the last command of `tox -e package`. CLAUDE.md's Layout block still lists only `docs/spec.md  docs/dev/adr/  docs/img/`, and neither the user-docs tree nor the wheel-completeness smoke appears anywhere in it, so the next contributor reading only CLAUDE.md does not know either exists.

**Fix:** Add `docs/user/` to CLAUDE.md's Layout block and one line noting that `tox -e package` ends by running tests/package_smoke.py with the clean venv's interpreter to prove templates, static files and the uk catalogue are in the wheel.
