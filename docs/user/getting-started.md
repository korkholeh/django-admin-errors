# Getting started

This walks through installing `django-admin-errors` into an existing Django project and seeing the
first captured error in the admin. For the package's public API and settings, see the project
[README](../../README.md); this page only covers what you, the operator, click through.

## 1. Install and enable

Ask your developer to add `admin_errors` to `INSTALLED_APPS` and run `python manage.py migrate` —
this creates the `Issue`, `Event` and `IssueDailyCount` tables. Once that's done, capturing errors
needs nothing further: unhandled exceptions and error-level log messages are captured automatically.

## 2. Open the Errors section

Log in to `/admin/` as a user with the `admin_errors.view_issue` permission (superusers always have
it). You'll see an **Errors** app on the admin index, with one model: **Issues**. Click it — this is
the list you'll come back to whenever something breaks.

## 3. Confirm it's working

Ask your developer to run:

```sh
python manage.py errors_test
```

This deliberately raises one harmless test error through the real capture pipeline and prints a
direct link to the resulting issue. Open that link (or refresh the issue list) — you should see one
new issue of type `AdminErrorsTestError`, titled `admin_errors errors_test: this is a deliberate,
harmless test error.`, with an occurrence count of 1.

If nothing appears within a few seconds, see [Troubleshooting](troubleshooting.md).

## What you should now see

- One issue in the list, with a status badge (**Open**), an exception type, a culprit (the
  function that raised), and "first seen"/"last seen" timestamps.
- Clicking into it shows the full traceback, and — if your account has the
  `admin_errors.view_issue_context` permission — the request that triggered it (none, for the test
  command, since it wasn't triggered by a web request).

From here, real errors from your application will start appearing the same way, grouped by cause
rather than duplicated per occurrence. Continue to [Triaging issues](triage-issues.md).
