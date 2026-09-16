# Review — phase 9 round 1

**Verdict:** changes_requested

Phase 9 delivers the notifier, W003, the copy-as-text traceback and a complete Ukrainian catalogue, and every acceptance criterion has a named test that can genuinely fail. I re-ran the gate myself: 338 passed / 8 skipped on SQLite, ruff + format + django check + makemigrations --check all clean; msgfmt of the committed .po byte-matches the committed .mo, 95 msgids, no fuzzy entries, no empty msgstr outside the header. The throttle UPDATE-before-send design is right and the two-thread race test really exercises it. Two major findings block approval: (1) the "Copy as text" block has no template-level permission gate, which is the one enforcement rule ADR 0007 states explicitly and names this block for — both gates ended up in admin.py; (2) refresh_connections() gates the generic NOTIFY_BACKEND extension point on the EmailNotifier-specific _recipients(), so a non-email custom backend is silently never connected unless the host also sets ADMINS — I reproduced this, and the custom-backend test only passes because an autouse fixture sets ADMINS. Neither is expensive to fix and neither needs a query-budget change. PLAN.md T1-T13 are all checked with nothing marked [~] and no environment excuses; the two deviations from ARCHITECTURE.md (new textformat.py, in-suite .po reader instead of a makemessages subprocess) are logged in DECISIONS.md with reasons that hold. I did not re-run the e2e suite.

## [MAJOR] "Copy as text" has no template-level permission gate, contrary to ADR 0007
`src/admin_errors/templates/admin/admin_errors/issue/includes/traceback.html`

ADR 0007's enforcement rules state: "Gating happens in both the view and the template (perms.admin_errors.view_issue_context) — a permission on a view is not a permission on a template include, and the plain-text 'Copy as text' traceback must respect it too." Risk #2 repeats the same wording. The implementation's two gates both live in admin.py: _redact_payload() strips vars, and include_locals=can_view_context is passed to format_traceback_text (admin.py:258-261 and 337-348). traceback.html:4-8 renders the result under a bare `{% if ae_traceback_text %}` with no `perms` check, unlike the established pattern at includes/frame.html:8 (`{% if ae_can_view_context and perms.admin_errors.view_issue_context and frame.vars %}`). PLAN.md calls this "double gate, same shape as ADR 0007", but the shape ADR 0007 mandates is view + template, and what shipped is view + view. Consequence: this template include now carries locals-bearing text with no gate of its own, so it is only safe as long as every present and future caller remembers to redact first — the exact failure ADR 0007 names as "the most likely place for a future leak to be introduced". The two new tests cannot catch such a regression, because they exercise the view path that does the redaction.

**Fix:** Give the template a gate of its own, mirroring frame.html. E.g. in admin.py set two context values — ae_traceback_text (always built with include_locals=False) and ae_traceback_text_context (include_locals=True, only when can_view_context) — then in traceback.html render `{% if ae_can_view_context and perms.admin_errors.view_issue_context %}{{ ae_traceback_text_context }}{% else %}{{ ae_traceback_text }}{% endif %}` inside the <pre>. Alternatively make it a `takes_context=True` template tag that reads context["perms"] itself. Add a test asserting the no-locals branch is chosen when the template is rendered with perms lacking view_issue_context even if the payload still carries vars.

## [MAJOR] refresh_connections() gates the generic NOTIFY_BACKEND on EmailNotifier's recipient list
`src/admin_errors/notifications.py`

_sync_connection() (notifications.py, `should_connect = conf.NOTIFY_BACKEND is not None and reason in conf.NOTIFY_ON and bool(_recipients())`) makes a non-empty email recipient list a precondition for connecting the receivers at all. _recipients() is an EmailNotifier concern: spec §10 and §5 define NOTIFY_BACKEND as a dotted path to any notifier and NOTIFY_RECIPIENTS as "None -> settings.ADMINS emails", i.e. the email address list, not a master switch. Consequence: a host that sets NOTIFY_BACKEND to a webhook/Slack/logging notifier and (reasonably) configures no ADMINS and no NOTIFY_RECIPIENTS gets no receivers connected and no notifications, with no warning and no system check. I reproduced it against tests.settings:

  ADMINS empty, custom backend -> created listeners: False
  ADMINS set,   custom backend -> created listeners: True

test_custom_notify_backend_is_used passes only because the autouse `_admins` fixture sets ADMINS for the whole module, so the suite cannot see this. The dynamic connect/disconnect design itself is sound and well justified (p09-plan decision, protects the frozen test_storage.py budgets) — the defect is that the enablement predicate belongs to the backend, not to the dispatcher.

**Fix:** Move the recipient test into the backend. In _sync_connection, resolve the backend (import_string inside try/except ImportError, AttributeError -> treat as disabled) and consult an optional hook, defaulting to enabled: `enabled = getattr(backend_cls, "is_enabled", lambda: True)()`. Add `@staticmethod def is_enabled() -> bool: return bool(_recipients())` to EmailNotifier. tests/settings.py sets no ADMINS, so the default host stays disconnected and the 15/7/8 and 12/15 budgets do not move. Then add a test: custom backend + ADMINS=[] + NOTIFY_RECIPIENTS=None still receives the call.

## [MINOR] Under default settings a regression of a just-resolved issue is silently throttled away
`src/admin_errors/notifications.py`

notified_at is never cleared, so the throttle window spans the resolve. With the default NOTIFY_THROTTLE_SECONDS=3600, the canonical operator flow — error arrives, mail sent, operator resolves it, it regresses ten minutes later — produces no email at all, which is the opposite of the phase goal ("the operator hears about ... a regression once"). The evidence is in the tests themselves: test_regression_sends_one_email has to set NOTIFY_THROTTLE_SECONDS: 0 to see the mail, and test_notification_after_throttle_window only proves the regression mail after 3700s. This is spec-literal (spec §10: "send only if issue.notified_at is None or older than NOTIFY_THROTTLE_SECONDS") and was logged, so I am not calling it a blocker — but it deserves an explicit decision rather than falling out of the implementation.

**Fix:** Either clear notified_at in admin._apply_status() when the new status is RESOLVED (a regression after an explicit resolve is a distinct event an operator asked to hear about, and the throttle still covers repeat occurrences), or leave the behaviour and document it explicitly in the Phase 10 README/FAQ next to NOTIFY_THROTTLE_SECONDS so an operator is not surprised by the silence.

## [MINOR] Subject prefix and NOTIFY_BASE_URL prefix are asserted nowhere
`tests/test_notifications.py`

Spec §10 fixes the subject as `[<SITE or hostname>] New issue: ValueError in shop.views.checkout`. test_new_issue_sends_one_email_to_admins asserts only that "New issue:", the type and the culprit appear in the subject — the `[...]` prefix is never asserted, and the Site branch of _prefix() has zero coverage (no test installs django.contrib.sites) while sitting behind a bare `except Exception` that would silently swallow a real misconfiguration. Likewise PLAN.md T4 explicitly committed to asserting "the NOTIFY_BASE_URL + admin URL" in the body, but the test asserts only `reverse(...) in message.body` and never sets NOTIFY_BASE_URL, so the new setting's only behaviour is untested (test_notify_base_url_default only checks the default value and W001).

**Fix:** In test_new_issue_sends_one_email_to_admins assert `message.subject.startswith(f"[{socket.gethostname()}] ")`. Add a case with `override_settings(ADMIN_ERRORS={"NOTIFY_BASE_URL": "https://errors.example.com"})` asserting `"https://errors.example.com" + reverse(...) in message.body`. Optionally add a _prefix() unit test that patches django.apps.apps.is_installed to cover the Site branch.

## [MINOR] The console-email assertion can be satisfied by a stale server log
`e2e/test_notifications_i18n.py`

_server_log_contains() reads the whole of /tmp/admin-errors-e2e-server.log (Makefile:15) and returns True if the needles appear anywhere in it. `make e2e-up` is documented as idempotent and exits 0 when the server already answers, so on the normal re-run path the log still holds every line from previous runs — and because the test deliberately reuses the shared demo_app.views.keyerror issue, a "New issue: KeyError demo_app.views.keyerror" line from an earlier run satisfies the assertion without any mail leaving the process this time. The docstring claims a "fresh notification line"; only a from-scratch server start makes that true.

**Fix:** Capture `offset = SERVER_LOG_PATH.stat().st_size if SERVER_LOG_PATH.exists() else 0` before _hit_until_admitted, and have _server_log_contains seek to that offset and search only the tail.

## [MINOR] Copy button pins "Copied" on a double click, and a rejected clipboard write is swallowed
`src/admin_errors/static/admin_errors/admin_errors.js`

onCopyClick reads `var original = button.textContent` inside the .then(), i.e. after the label may already have been swapped. Click twice inside the 2s window and the second handler captures "Copied" as `original`; the first timer restores "Copy as text" at t=2s and the second overwrites it back to "Copied" at t=3s, leaving the button permanently labelled "Copied". Separately, `copyText(...).then(...)` has no rejection handler: navigator.clipboard.writeText rejects when the document is not focused or permission is denied, which yields an unhandled promise rejection, no label feedback, and no execCommand fallback — the fallback only runs when the Clipboard API is entirely absent.

**Fix:** Read the original label once before copying (or keep it in a data attribute, e.g. data-ae-label, set on first use) and clear any pending timer via a handle stored on the button before starting a new one. Add `.catch(function () { /* fall back to execCommand path or leave the label */ })` so a rejected write still gives feedback and never raises an unhandled rejection.

## [NIT] to_have_text("Copied") races the 2s revert timer
`e2e/test_notifications_i18n.py`

test_copy_as_text_flips_to_copied asserts the flipped label with Playwright's polling expect() against a label that reverts after 2000ms. It will normally win the race, but on a loaded machine (or after the retry loop in the other case has warmed the box) a first poll landing past the revert makes it fail for no product reason.

**Fix:** Either raise the revert delay behind a data attribute the test can read, or assert on a state that does not self-clear (e.g. toggle a `data-ae-copied-state="1"` attribute alongside the label change and assert on that), keeping the visible 2s label purely cosmetic.

## [NIT] W003 assumes LOGGING's handlers/loggers/root values are dicts
`src/admin_errors/checks.py`

check_mail_admins_overlap calls handler_configs.items(), logging_config.get("root", {}).get(...) and logger_config.get("handlers", []) with no type guard, so a malformed LOGGING setting (handlers as a list, a logger entry as a string) raises out of a system check instead of being reported. Same shape as the existing W002 helper, so it is consistent rather than new — but W003 is the check a host hits precisely while rearranging its LOGGING dict.

**Fix:** Guard with isinstance(..., dict) / isinstance(..., (list, tuple)) before iterating, returning [] on anything unexpected; dictConfig itself will report the real malformation.
