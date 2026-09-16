# Using the Errors section of the admin

`django-admin-errors` adds an **Errors** section to your Django admin. Every unhandled exception and
every error-level log message in your site is recorded there, grouped so that the same bug seen a
thousand times is one entry with a count of 1000 — not a thousand entries. Each one shows the
traceback, which request triggered it, when it was first and last seen, and a 14-day trend.

Nothing leaves your servers: the errors are stored in your project's own database and read back
through the admin you already log in to.

This guide is for the person who logs in and looks at the issues. For the developer installing and
configuring the package, the project [README](../../README.md) is the reference.

## Getting it installed

Installation takes a developer about five minutes and is two steps: add `admin_errors` to
`INSTALLED_APPS` and run `python manage.py migrate`. There is nothing to configure — capture starts
immediately. The exact commands are in [Getting started](getting-started.md) and, in more detail,
in the project [README](../../README.md).

## Read these in this order

1. **[Getting started](getting-started.md)** — install, find the **Errors** section, and confirm
   capture works by making one deliberate test error appear. Start here even if someone else did
   the install.
2. **[Triaging issues](triage-issues.md)** — the day-to-day page. How to read the issue list and
   the detail page, and what Resolve, Ignore and Reopen do.
3. **[Notifications](notifications.md)** — who gets emailed about a new or returning error, how
   often, and how to turn it off.
4. **[Retention](retention.md)** — how much history is kept, why the occurrence count can be larger
   than the number of stored occurrences, and how to clean up on demand.
5. **[Troubleshooting](troubleshooting.md)** — read this when errors you know happened are not
   showing up, or a page is missing detail you expected to see.
