# Notifications

By default, the first time a new kind of error happens, and again the first time a **resolved**
issue comes back (a "regression"), an email is sent — so you find out about new problems without
having to check the admin constantly, but without being flooded by every single occurrence of the
same error.

## Who receives it

Whoever is listed in the project's `ADMINS` setting, unless your developer has configured a
different recipient list specifically for these emails.

## The throttle

If the same issue keeps happening, you get at most one email per hour for it (configurable) — not
one per occurrence. Resolving an issue does **not** reset this timer: if it regresses again very
soon after being marked resolved, you may not get a second email until the hour is up. The issue's
badge on the detail page will still show **Regressed** even if no email was sent.

## Turning it off

Ask your developer to set `ADMIN_ERRORS = {"NOTIFY_BACKEND": None}` in the project's settings to
disable these emails entirely, or `NOTIFY_ON = ["created"]` to only be notified about brand-new
issues and never about regressions.

## If you're also getting Django's own error emails

Django has its own, older mechanism (`AdminEmailHandler`) that emails admins on **every single**
occurrence of a server error, with no deduplication. If that's still configured alongside this
package, you may be getting two emails for the same error — one deduplicated from
`django-admin-errors`, one per-occurrence from Django itself. Ask your developer to remove the older
handler now that this package's notifications cover the same need (the project's `python manage.py
check` will flag this overlap for them).
