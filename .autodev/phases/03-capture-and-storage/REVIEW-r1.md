# Review — phase 3 round 1

**Verdict:** changes_requested

Implementation is solid: guards, savepointed creates, per-block try/except, and the degradation ladder all match the plan, and the gate is genuinely green (verified myself: 105 passed, full lint command exit 0, migration untouched, all PLAN tasks [x] with no [~]). But three acceptance criteria are not actually proven, two of them privacy-relevant (risk #2). (1) An undecorated POST field named `password` is stored in clear text — verified `build_request_block(rf.post('/x/',{'password':'hunter2'}))['post']` returns `{'password':'hunter2'}` — and both "scrubbing" tests plant `request.sensitive_post_parameters` by hand, so they test the same branch and the decorated view is never called. (2) The `Authorization` header is not redacted on Django 4.2, a supported target: 4.2's `hidden_settings` is `API|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE` (no `AUTH`, added in 5.x), so the header test will fail that tox leg and a bearer token lands in the DB; the `Cookie` header criterion has no test at all. (3) `test_ignored_exception_types_are_dropped` cannot fail — instrumenting `capture._capture_record` shows `/notfound/` and `/denied/` never reach the pipeline (root handler is ERROR, Django logs 404/403 at WARNING), leaving `IGNORE_EXCEPTIONS` and its dotted-path/subclass logic untested. Minors: `CAPTURE_LEVEL` below WARNING is a silent no-op (root logger level stays 30), every payload stores the outermost frame list twice (measured 2591 B duplicated in a 5854 B payload, halving the effective `MAX_PAYLOAD_BYTES`), a production `assert` in the racing-create path, an untested recursion half of the storage-failure criterion, and query budgets (16/8/10 vs observed 10/3/5) too loose to catch the N+1 risk #9 names — PLAN T8 flagged these to tighten in T12 and T12 did not.

## [MAJOR] POST `password` criterion proven by a faked precondition; undecorated POST passwords are stored in clear text
`tests/test_context.py`

`test_post_password_value_is_absent_from_the_payload` (line 31) and `test_sensitive_post_parameters_values_are_absent_from_the_payload` (line 39) both set `request.sensitive_post_parameters = ["password"]` by hand, so they exercise exactly the same branch of Django's `get_post_parameters` and neither proves the first criterion. The decorated view `tests/views.py:62 sensitive_post` is never called by any test (test_context calls `views.sensitive_vars`/`views.nested` directly; `/sensitive-post/` only appears as an `rf.post()` URL string), so the `@sensitive_post_parameters` integration is untested too. Verified the real behaviour: `context.build_request_block(rf.post('/x/', {'password':'hunter2'}))['post']` returns `{'password': 'hunter2'}` — a host that POSTs a password to a view without the decorator gets it persisted in `Event.payload` and rendered to anyone with `view_issue_context`. Django's `SafeExceptionReporterFilter.get_post_parameters` returns `request.POST` unchanged when the attribute is absent. Spec §7.2.6 prescribes exactly this delegation, but spec §2 (line 42) promises 'passwords ... are scrubbed' and spec §14 (line 727) lists 'POST `password`' as a case distinct from '`@sensitive_post_parameters`'.

**Fix:** Drive `/sensitive-post/` end to end through `Client(raise_request_exception=False)` so the real decorator sets the attribute, and make the two tests assert different things. Then resolve the spec ambiguity explicitly in DECISIONS.md: either apply key cleansing to POST as well via `filter_.cleanse_setting(key, value)` (still Django's own filter, so the never-hand-roll pitfall is respected), or record that undecorated POST fields are intentionally kept verbatim and correct spec §2/§14.

## [MAJOR] `Authorization` header is not redacted on Django 4.2; the header test will fail the tox matrix
`src/admin_errors/context.py`

`build_request_block` (lines 291-300) relies entirely on `filter_.get_safe_request_meta`. Verified from `stable/4.2.x/django/views/debug.py`: `SafeExceptionReporterFilter.hidden_settings` is `API|TOKEN|KEY|SECRET|PASS|SIGNATURE|HTTP_COOKIE` — `AUTH` only arrived in 5.x (the installed 5.2.17 has it). So on Django 4.2, a supported target in `tox.ini`, `HTTP_AUTHORIZATION` matches nothing and the bearer token is stored verbatim; `tests/test_context.py:57 test_sensitive_headers_are_absent_from_the_payload` asserting `'super-secret-token' not in dumped` will fail that leg. DECISIONS (p03-implement/context) records this as accepted upstream behaviour, but the phase's own acceptance criterion and spec §14 line 727 both require the `Authorization` header scrubbed on every supported version. Separately, the criterion also names the `Cookie` header and no test covers it — it happens to work (`HTTP_COOKIE` is in the pattern on both 4.2 and 5.2), but `test_sessionid_cookie_value_is_absent_from_the_payload` only inspects `block['cookies']`, never `block['headers']['Cookie']`.

**Fix:** After `get_safe_request_meta`, pass each retained header key through `filter_.cleanse_setting(key, value)` with an extended pattern (or an explicit key list covering `AUTH`) so the result is version-independent, and add `assert 'super-secret-session' not in json.dumps(block['headers'])` to the cookie test. If instead 4.2 is to be dropped, remove it from tox and the CLAUDE.md stack line.

## [MAJOR] `test_ignored_exception_types_are_dropped` cannot fail; `IGNORE_EXCEPTIONS` is untested
`tests/test_capture.py`

Line 120 requests `/notfound/` and `/denied/` through the test client and asserts `Issue.objects.count() == 0`. Verified by instrumenting `capture._capture_record` around those two requests: it is never called (`emit reached with records: []`). Django logs 404/403 via `django.request` at WARNING, and the root handler is installed at `CAPTURE_LEVEL="ERROR"`, so the record is filtered before the pipeline is entered; even if it were not, `IGNORE_HTTP_STATUS_BELOW=500` would drop it first. The test therefore passes with `IGNORE_EXCEPTIONS` set to `[]`. `capture._is_ignored_exception_type` and `_resolve_ignore_exception_types` (capture.py:84-102, including the dotted-path cache and the `issubclass` subclass rule) have no test that can fail.

**Fix:** Test the branch directly: `assert api.capture_exception(Http404('x')) is None` and the same for `PermissionDenied`, with a positive control under `override_settings(ADMIN_ERRORS={'IGNORE_EXCEPTIONS': []})` showing an Issue is created; add a subclass case (`class MyNotFound(Http404)`) and an unresolvable dotted path to pin the cache fallback.

## [MINOR] `CAPTURE_LEVEL` below WARNING is a silent no-op
`src/admin_errors/apps.py`

`_install_logging_handler` sets the handler level from `CAPTURE_LEVEL` but never touches the root logger, whose default level is WARNING (verified: `logging.getLogger().level == 30`). A host setting `CAPTURE_LEVEL="INFO"` or `"DEBUG"` therefore captures nothing, with no warning — spec §5 line 153 describes it as 'Minimum log level captured by the logging handler'. This is exactly risk #12 (capture silently disabled).

**Fix:** In `_install_logging_handler`, when the numeric `CAPTURE_LEVEL` is below the root logger's effective level, lower the root level to match (or add a `W00x` check reporting that the configured level is unreachable) and cover it with a test.

## [MINOR] Every payload stores the outermost frame list twice
`src/admin_errors/context.py`

`build_payload:395` sets `payload["frames"] = exception_block["chain"][0]["frames"]`. In memory it is the same object, but it serialises twice: measured on `/boom/`, the stored `Event.payload` is 5854 bytes of which the 2591-byte frame list appears both at the top level and in `chain[0]` — ~44% duplication, higher with deeper stacks and `CAPTURE_LOCALS`. `_enforce_size` measures the duplicate too, so the effective `MAX_PAYLOAD_BYTES` budget is roughly halved. Spec §6.4 does document both keys, so this is a schema question rather than a coding error, but it doubles storage growth for the common single-exception case.

**Fix:** Either omit `chain[i]["frames"]` when `len(chain) == 1` (the top-level key already carries them), or keep the spec shape and note the duplication next to `MAX_PAYLOAD_BYTES` so the default 64 KiB is read correctly.

## [MINOR] Bare `assert` used for control flow in the racing-create path
`src/admin_errors/storage.py`

`_create_issue:96` does `assert row is not None` after re-reading following an `IntegrityError`. Under `python -O` the assert is stripped, `None` is returned, and `_store_one:52` raises `TypeError: 'NoneType' object is not subscriptable`. It also mislabels the failure mode: any non-unique `IntegrityError` (a NOT NULL or FK violation from a malformed meta dict) takes the same branch and re-selects nothing, so the real cause is lost.

**Fix:** Replace with an explicit re-raise: `row = _select_issue(...)` then `if row is None: raise` inside the `except IntegrityError:` block, so an unexpected integrity error propagates with its original traceback (the capture pipeline's own `try/except BaseException` still contains it).

## [MINOR] The 'never recurses' half of the storage-failure criterion is not exercised
`tests/test_capture.py`

`test_storage_failure_does_not_propagate_or_recurse:206` asserts only that the request returns 500 and no Issue exists. With `INTERNAL_LOGGING=False` (the default), `_log_internal_once` returns immediately, so the one path that could actually re-enter the pipeline after a storage failure — emitting a WARNING to `admin_errors.internal` through the root handler — never runs in any test. Nothing asserts `store_batch` was called exactly once.

**Fix:** Wrap the test in `override_settings(ADMIN_ERRORS={'INTERNAL_LOGGING': True})`, count invocations in the monkeypatched `store_batch`, and assert the count is exactly 1 — that pins both the `admin_errors.*` logger drop and the recursion guard.

## [MINOR] Query budgets are too loose to catch the N+1 they guard
`tests/test_storage.py`

PLAN.md T8 explicitly marks these as 'generous placeholders (not yet tightened to the observed counts) — worth revisiting in T12's polish pass', and T12 did not revisit them. Observed counts recorded in the comments are 10 / 3 / 5 against bounds of 16 / 8 / 10. Replacing `bulk_create` with a per-row insert loop on the three-sample path takes it from 5 to 7 queries, still comfortably under 10, so the bound cannot detect the regression risk #9 names as its reason for existing.

**Fix:** Tighten to observed + 1 or 2 (e.g. 12 / 4 / 6), leaving room for the extra PostgreSQL savepoint statements the plan mentions but not for a per-row or per-date loop.

## [NIT] Dead entries in the test host
`tests/views.py`

`boom_with_id` (and its `/boom/<int:pk>/` route) is referenced by nothing. `unrepresentable` and `sensitive_post` are also never invoked — `test_context.py` defines its own `_Unrepresentable` class inline and fakes the `sensitive_post_parameters` attribute rather than calling the decorated view, so `/unrepresentable/` and `/sensitive-post/` are unreachable fixtures.

**Fix:** Wire `sensitive_post` and `unrepresentable` into real client-driven tests (the former is the fix for the first finding), and drop `boom_with_id` and its route until a test needs a parameterised URL.
