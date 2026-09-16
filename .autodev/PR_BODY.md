# django-admin-errors 0.1.0

A Django project can now `pip install django-admin-errors`, add one entry to `INSTALLED_APPS`, run
`migrate`, and get a Sentry-style **Errors** section in its own admin — every unhandled exception and
error-level log record captured into its own database, grouped into issues by fingerprint, with
traceback, request context, occurrence counts, 14/30-day trends and Resolve/Ignore/Reopen — with no
external service, no extra infrastructure and no runtime dependency beyond Django.

## Why

Sentry is the right answer until it is not: data-residency rules, air-gapped deployments, policy
bans, or a small internal tool where the operational cost of a second system is larger than the
system it watches. The target user already lives in the Django admin and already runs a database.
This ships the 80 % of issue triage that matters into the place they already are, and is explicit
about the 20 % it does not do — no tracing, no breadcrumbs, no release tracking, no alert-rule
engine, no REST API, no multi-tenancy.

Built over 10 phases from `docs/spec.md`. Every check is green: `uv run pytest -q` (357 passed, 8
PostgreSQL-only skips), `make test-pg` (365 passed), coverage 91 %, the four-command lint gate,
`make e2e-up && uv run --extra e2e pytest e2e -q` (25 passed, 1 deselected by design), `tox -e
package` including a clean-venv wheel smoke test, and `python -m build` + `twine check`.

## Worth a close look

**Secret handling is the whole compliance promise** — `src/admin_errors/context.py`,
`src/admin_errors/admin.py`, `templates/`. Payloads carry production headers, cookies, POST bodies
and frame locals. Two things to check rather than take on trust: that scrubbing really is delegated
to Django's `get_exception_reporter_filter(request)` and nowhere hand-rolled, and that the
`view_issue_context` permission is enforced in *both* the view (`_redact_payload` strips the keys
before the template sees them) and every template include, including the *Copy as text* traceback.
`tests/test_admin.py` asserts the sensitive strings are absent from the response body, not merely
that a block is hidden — but a missed include would be a silent leak, and there are several includes.
One deliberate call worth confirming: request **method and path** are readable with `view_issue`
alone; only the rest of the request block needs `view_issue_context`.

**The migration is a permanent public contract** — `src/admin_errors/migrations/0001_initial.py`.
Exactly one migration ships, and it installs into other people's production databases. Everything
after this release must be additive. Review the indexes, the unique constraints and the
`view_issue_context` permission declaration now, because changing them later means writing a data
migration for every installation.

**The fingerprint algorithm cannot be changed after release** —
`src/admin_errors/fingerprint.py`, ADR 0003. It decides what is "the same error". Changing the
normalisation order or any of its inputs duplicates every existing issue in every installation, so
it is a major-version change from here on. It is pinned by golden-value tests; the normalisation
order (UUID → ISO-8601 → `0x` address → hex run → quoted literal → digit run → whitespace) is the
part to argue about, and now is the only cheap time to do it.

**Capture runs on an already-failing request and must not make it worse** —
`src/admin_errors/capture.py`, `src/admin_errors/writer.py`. The whole pipeline is inside
`try/except BaseException` with `KeyboardInterrupt`/`SystemExit` re-raised, there is no I/O and no
shared lock on the host's thread, `enqueue` is `put_nowait`, and a fork is detected on every
`enqueue()` by pid so gunicorn `--preload` cannot inherit a dead writer thread. If any of that is
wrong, the failure mode is "the error tracker took the site down", which is the one outcome the
product cannot survive.

**Two accepted notification trade-offs that a reviewer may want reversed** —
`src/admin_errors/notifications.py`. (a) `notified_at` is set by a conditional `UPDATE` *before* the
email is sent: that is what makes concurrent workers send exactly once, and it means a failed SMTP
send loses that notification with no retry. (b) Resolving an issue does not clear `notified_at`, so
an issue that regresses inside the one-hour throttle window shows the **Regressed** badge but sends
no email. Both were raised in review and both were deliberately kept; the reasoning is in
`.autodev/DECISIONS.md` under `p09-review_fix1` and `p09-review_fix2`.

**A demo-only test hook** — `demo/demo_app/views.py::reset_rate_limits`, a `DEBUG`-gated, POST-only
endpoint that clears the in-process sampling buckets so e2e runs are repeatable. `demo/` is never
packaged, so it cannot reach an installation, but it is a test hook in an app directory and is worth
confirming as such.

**Incomplete: the browser suite is the fragile part.** Several e2e cases share the `/boom/`
fingerprint, whose `EVENT_SAMPLE_PER_HOUR` bucket is process-global on the long-lived demo server.
The suite is green on a freshly started server, but re-running it against the same process can fail
cases that just passed. A session-scoped reset fixture and one test moved to its own fingerprint are
the mitigations in place; a per-module fresh server is the real fix and was not done. Also
deliberately unassertable: the dark-mode readability check, which is covered only by the four
screenshots in `docs/img/`.

See `.autodev/HANDOFF.md` for the full state, the decisions log and what to do first.
