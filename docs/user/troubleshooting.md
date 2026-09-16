# Troubleshooting: "it did not work"

Work through these in order. Each one points at a command your developer can run to confirm the
cause — the project [README](../../README.md)'s FAQ has the full technical detail behind each check.

1. **No issues appear at all, even for errors you know happened.** Ask your developer to run
   `python manage.py errors_test`. It raises one deliberate test error through the real pipeline and
   either shows you a link to the resulting issue, or prints exactly why nothing was captured (a
   setting turned it off, the log level is too high, etc.).
2. **`errors_test` works, but a specific real error still doesn't show up.** It may not be
   considered an error at all — 404s and permission-denied responses are never captured on purpose,
   and log messages below the configured minimum level (`ERROR` by default) are skipped. Ask your
   developer to check `ADMIN_ERRORS["CAPTURE_LEVEL"]` and whether the code path in question actually
   raises or logs at that level.
3. **Issues appear, but you can't see the request details (headers, POST data, who was logged in).**
   That section is gated behind a separate permission, `admin_errors.view_issue_context`, on top of
   the base `admin_errors.view_issue` permission — ask whoever manages admin permissions to grant it
   if you're supposed to see that detail.
4. **Too many issues, or the same problem seems to create a new issue every time.** Ask your
   developer to run `python manage.py errors_stats` for a full picture (row counts, status
   breakdown, size), and see [Retention](retention.md) for the automatic limits that keep this in
   check.
5. **You're not getting the "new issue" email you expected.** See [Notifications](notifications.md)
   — check that `ADMINS` (or the configured recipient list) is actually set, and that the throttle
   window hasn't already been used for that particular issue.
6. **Still stuck.** Have your developer check `python manage.py check` — the system checks
   (`admin_errors.W001`–`W003`, `E001`–`E002`) catch the most common misconfigurations and print a
   plain-English explanation of each one.
