# Phase 9 — Notifications, status transitions and i18n

Goal: the operator hears about a new issue or a regression **once**, not on every occurrence, and the
UI is fully translatable with a Ukrainian catalogue that ships in the wheel.

## Context

### What already exists

| Piece | State | Where |
|---|---|---|
| `issue_created` / `issue_regressed` | Fired post-commit from `storage._store_one` via `transaction.on_commit(partial(_fire, …))`; `_fire` re-reads the `Issue` row **only when `signal.has_listeners()`** (p04-plan decision, protects the `test_storage.py` query budgets 15/7/8) | `src/admin_errors/storage.py:71-81,166-172` |
| `signals.send_safely` | `send_robust` + `writer.stats.receiver_errors` + recursion guard held for the dispatch | `src/admin_errors/signals.py:31-45` |
| `issue_status_changed` | Already fired from `_apply_status()`, which backs **both** the single-issue status view and the three bulk actions; idempotent (no signal when the status does not change) | `src/admin_errors/admin.py:50-71,363-368` |
| `Issue.notified_at` | Column exists in `0001_initial`, read into `_select_issue`'s `.values()`, never written | `models.py:40`, `storage.py:86,122` |
| Notification settings | `NOTIFY_BACKEND` (default `"admin_errors.notifications.EmailNotifier"`), `NOTIFY_RECIPIENTS`, `NOTIFY_THROTTLE_SECONDS`, `NOTIFY_ON` present in `conf.DEFAULTS`; **`NOTIFY_BASE_URL` is missing** | `conf.py:66-69` |
| `notifications.py` | **Does not exist** — the default `NOTIFY_BACKEND` dotted path currently points at nothing | — |
| System checks | `W001`, `W002`, `E001`, `E002` implemented and registered in `ready()`; **`W003` missing** | `checks.py`, `apps.py:22-27` |
| Admin templates | 13 templates, **all already `{% load i18n %}` + `{% translate %}`** (Phase 8); `gettext_lazy` used in `apps.py`, `admin.py`, `models.py`, `templatetags/admin_errors_tags.py` | `src/admin_errors/templates/admin/admin_errors/issue/` |
| `locale/` | **Does not exist**, although `pyproject.toml` already declares `src/admin_errors/locale/**/*.mo` as a wheel artifact | `pyproject.toml:60-64` |
| "Copy as text" | **Does not exist** — explicitly deferred from Phase 8 to this phase (p08-plan decision) | — |
| View-level permission gate | `_redact_payload(payload, can_view_context)` strips `vars`, gated request keys, `celery`, `extra` before the payload reaches any template (ADR 0007 double gate) | `admin.py:74-101,249-276` |
| Demo host | Already has `ADMINS = [("Demo Admin", "admin@example.com")]` and the console email backend, so notifications become visible in the demo the moment `notifications.py` lands | `demo/demo_project/settings.py:105-106` |
| Assets | `admin_errors.css` 2 816 / 6 144 B, `admin_errors.js` 425 / 3 072 B (bounds asserted in `test_admin.py:829-830`) | `static/admin_errors/` |
| gettext toolchain | `xgettext`/`msgfmt`/`msgmerge` present (`/opt/homebrew/bin`, GNU gettext-tools) | verified at plan time |

### What this phase changes

1. New `notifications.py` (`EmailNotifier` + the signal plumbing) and new `textformat.py` (one plain-text
   traceback renderer shared by the email body and the "Copy as text" block).
2. `conf.py` gains `NOTIFY_BASE_URL`; `checks.py` gains `W003`; `apps.ready()` wires the notifier.
3. `admin.py` + `traceback.html` + `admin_errors.js`/`.css` gain the "Copy as text" block.
4. `locale/uk/LC_MESSAGES/django.po` + `.mo` land, after a sweep that confirms no UI string is
   unwrapped.
5. New `tests/test_notifications.py`; additions to `tests/test_admin.py` (copy-as-text gating, `uk`
   rendering, catalogue completeness) and `tests/test_settings_and_checks.py` (`W003`,
   `NOTIFY_BASE_URL`).

No migration: `notified_at` already exists, and this phase must ship none.

## Design

### `notifications.py`

```python
class EmailNotifier:
    def notify(self, *, issue: Issue, event: Event | None, reason: str) -> bool: ...
    # reason in {"created", "regressed"}; returns True when a mail was sent
```

`notify()` order of operations (matches `[architect/design]` in DECISIONS.md and RISKS row #13):

1. `recipients = _recipients()` — `NOTIFY_RECIPIENTS` if set, else the emails from
   `django_settings.ADMINS`. Empty → return `False` (nothing to send, no DB write).
2. Throttle **before** sending, one statement, on the configured alias:
   ```python
   updated = (
       Issue.objects.using(conf.DATABASE)
       .filter(pk=issue.pk)
       .filter(Q(notified_at__isnull=True) | Q(notified_at__lt=now - timedelta(seconds=conf.NOTIFY_THROTTLE_SECONDS)))
       .update(notified_at=now)
   )
   if not updated:
       return False
   ```
   Only the worker whose `UPDATE` touched a row sends — that is the concurrency contract, and it is
   why the update precedes the send. A failed send is *not* retried (accepted, already decided).
3. `send_mail(subject, body, from_email=None → DEFAULT_FROM_EMAIL, recipients, fail_silently=False)`.
   `send_mail` raising is fine: the notifier is invoked through `send_safely`, so `send_robust`
   isolates it and `writer.stats.receiver_errors` counts it.

Subject: `[{prefix}] New issue: {ValueError} in {culprit}` / `… Regression: …`, where `prefix` is
`Site.objects.get_current().name` when `django.contrib.sites` is installed (wrapped, falling back on
any exception) else `socket.gethostname()`. Title part is `f"{issue.exception_type} in {issue.culprit}"`,
falling back to `issue.title` for a message-only issue with no `exception_type`.

Body (plain text, `gettext` labels):

```
New issue in <prefix>

Title:       <issue.title>
Culprit:     <issue.culprit>
Level:       <issue.level>
Occurrences: <issue.count>
First seen:  <issue.first_seen isoformat>
Last seen:   <issue.last_seen isoformat>

<short traceback: frames only, no locals, innermost last, capped at 10 frames>

<NOTIFY_BASE_URL><reverse("admin:admin_errors_issue_change", args=[issue.pk])>
```

The admin URL is built in a `try/except NoReverseMatch` (a host may run `ADMIN_SITE=False` or not
install the admin at all); on failure the line is omitted rather than the mail lost.

### Connection lifecycle (the one non-obvious part)

`notifications.refresh_connections()` connects two thin receivers
(`_on_issue_created` / `_on_issue_regressed`, each with a `dispatch_uid`) **iff** all of:
`NOTIFY_BACKEND is not None`, the matching `reason` is in `NOTIFY_ON`, and `_recipients()` is
non-empty. Otherwise it disconnects them. It is called from `apps.ready()` and from a
`setting_changed` receiver in `notifications.py` (which calls `conf.settings.reset()` first, so it
cannot read a stale cache regardless of receiver order).

Why not simply connect one always-on dispatcher that resolves `NOTIFY_BACKEND` per call:
`storage._fire` deliberately skips re-reading the `Issue` row while `has_listeners()` is `False`
(p04-plan), so an always-on receiver would add a `SELECT` to the create path of *every* aggregate in
*every* test, including the `test_storage.py` budgets, which must never be relaxed (rule 9). Dynamic
connect/disconnect keeps `NOTIFY_BACKEND=None` and the recipient-less default test host at exactly
zero cost, and keeps `override_settings` working — which is the whole reason `conf.settings` exists.

The backend itself is resolved per signal via `import_string(conf.NOTIFY_BACKEND)` and instantiated
with no arguments, so a custom dotted path is honoured without a reconnect and needs no registry.

### `textformat.py` (new module — deviation, justified)

`format_traceback_text(payload, *, include_locals=False, max_frames=None) -> str` renders a
CPython-style plain traceback from a §6.4 payload:

```
Traceback (most recent call last):
  File "/app/views.py", line 42, in process_payment
    raise RuntimeError(message)
    token = '…'                      # only when include_locals
builtins.RuntimeError: process payment failed
```

Chained entries are rendered root-cause-first with the same separators the HTML traceback uses, by
reusing the ordering already implemented for the template tag. Pure function, no Django imports
beyond `gettext`.

`ARCHITECTURE.md`'s module table does not list this module. It is added rather than folded into
`notifications.py` or `templatetags/admin_errors_tags.py` because both consumers need it and neither
should import the other's layer (the admin would otherwise import the mail layer, or the mail layer
the template-tag layer). Logged in DECISIONS.md.

### "Copy as text"

`change_view` and `event_detail_view` already compute `ae_payload` through `_redact_payload`. Each
adds:

```python
extra_context["ae_traceback_text"] = textformat.format_traceback_text(
    extra_context["ae_payload"], include_locals=can_view_context
)
```

Double gate, same shape as ADR 0007: the payload handed to the formatter has already lost its `vars`
for a `view_issue`-only user, **and** `include_locals` is `False` for that user — either alone
suffices. `traceback.html` renders `<pre id="ae-traceback-text" hidden>{{ ae_traceback_text }}</pre>`
plus a `<button type="button" class="ae-copy" data-ae-copy-target="ae-traceback-text"
data-ae-copied="{% translate 'Copied' %}">{% translate "Copy as text" %}</button>`. The JS extends the
existing delegated `click` listener: `navigator.clipboard.writeText()` when available, else a hidden
`<textarea>` + `document.execCommand("copy")`; the button label switches to the `data-ae-copied`
string for 2 s. No inline script (CSP), the translated strings come from `data-` attributes, and the
asset bounds (6 KiB / 3 KiB) stay asserted.

### i18n

Templates and Python UI strings are already marked (Phase 8 did it); this phase (a) sweeps for
misses — including the new notification subject/body labels and the new button labels, (b) runs
`makemessages -l uk` from `src/admin_errors/`, (c) translates every `msgid`, (d) runs
`compilemessages`. `LANGUAGE_CODE` stays `en-us` everywhere; the `uk` render test activates the
language with `override_settings(LANGUAGE_CODE="uk")`.

Catalogue completeness is asserted by a test that parses the committed `.po` with a ~25-line reader
(no `polib` — zero runtime *and* test deps beyond the declared extras) and requires a non-empty
`msgstr` for every non-header `msgid`, including every plural form. A second test activates `uk` and
asserts a known string differs from its English source, which is what proves the `.mo` is compiled
and loadable. `makemessages` itself is not run inside the suite (it would make the gate depend on
GNU gettext being installed on every matrix cell); it is run by hand in T9 and its output must show
no new entries.

### Error handling summary

| Failure | Behaviour |
|---|---|
| SMTP down / slow | Exception isolated by `send_robust`, counted in `writer.stats.receiver_errors`, reported once per type via `capture._log_internal_once`. `notified_at` already moved, so no retry storm. README (Phase 10) recommends a queued backend. |
| `NOTIFY_BACKEND` dotted path invalid | `ImportError` surfaces inside the receiver → swallowed and counted, capture unaffected. |
| No recipients | No mail, no DB write, receivers not even connected. |
| `reverse()` unavailable | URL line omitted. |
| Payload missing / `v` unknown | Formatter tolerates missing keys and returns the message line only. |

## Tasks

- [x] **T1** — `conf.py`: add `"NOTIFY_BASE_URL": ""` to `DEFAULTS` (spec §10 names it; it is the only
  missing key). Tests in `tests/test_settings_and_checks.py`: default is `""`, and setting it in
  `ADMIN_ERRORS` produces no `W001`.
- [x] **T2** — new `src/admin_errors/textformat.py` with `format_traceback_text()`. Tests in
  `tests/test_notifications.py`: frames innermost-last with `File "…", line N, in func`, chained
  entries with separators, `include_locals=True` shows a local and `include_locals=False` never does,
  `max_frames` keeps the innermost N, a message-only payload and `None` both render without raising.
- [x] **T3** — new `src/admin_errors/notifications.py`: `EmailNotifier.notify()` (recipients → throttle
  `UPDATE` → `send_mail`), subject/body builders, `_recipients()`, `refresh_connections()`, the two
  receivers and the `setting_changed` hook. Wire `refresh_connections()` from `apps.ready()` (no DB
  access, no thread, no filesystem — it only connects signals).
- [x] **T4** — `tests/test_notifications.py` (spec §14.2): new issue → exactly one mail to `ADMINS`,
  asserting subject prefix/`New issue:`/type/culprit and every body field (title, culprit, count,
  first/last seen, a traceback frame line, the `NOTIFY_BASE_URL` + admin URL); regression → one mail
  with `Regression:`; ignored issue → none; second occurrence inside the window → none; after the
  window (frozen clock) → one; `NOTIFY_RECIPIENTS` overrides `ADMINS`; `NOTIFY_ON=["regressed"]`
  suppresses the created mail; `NOTIFY_BACKEND=None` → no mail and no listeners; a custom dotted-path
  backend is loaded and receives the call; a backend raising does not break `store_batch` and
  increments `writer.stats.receiver_errors`.
- [x] **T5** — concurrency case in `tests/test_notifications.py`: `transaction=True`, two threads calling
  `notify()` for the same issue → exactly one mail and one `notified_at`. Follows the pattern of
  `test_storage.py`'s two-thread race case (barrier, `connections.close_all()` in each thread).
- [x] **T6** — `checks.py`: `W003_ID` + `check_mail_admins_overlap()` — a `Warning` when
  `NOTIFY_BACKEND` is not `None` and the host's `LOGGING["handlers"]` contains a handler whose
  `class` ends with `AdminEmailHandler` that is referenced from `root` or any logger's `handlers`.
  Register it in `apps.ready()`. Tests in `tests/test_settings_and_checks.py`: triggers with such a
  `LOGGING` dict; silent when `NOTIFY_BACKEND=None`; silent when the handler is defined but
  unreferenced; silent with no `LOGGING`.
- [x] **T7** — "Copy as text": `ae_traceback_text` in `change_view` and `event_detail_view`, the
  `<pre hidden>` + button in `includes/traceback.html`, the copy handler in `admin_errors.js`, minimal
  `.ae-copy` styling in `admin_errors.css`. Tests in `tests/test_admin.py`: present for
  `view_issue_context` and contains `SECRET_LOCAL`; present for `view_issue`-only and contains neither
  `SECRET_LOCAL` nor any `vars` line; same two cases on the event-detail page; assets still within the
  6 KiB / 3 KiB bounds (existing assertions).
- [x] **T8** — status transitions: confirm `_apply_status()` is the only mutation path and add the
  missing coverage in `tests/test_admin.py` — the **bulk** action path fires `issue_status_changed`
  once per issue with the right `old_status`/`new_status`/`user` (the single-issue path and
  idempotency are already covered at `test_admin.py:659,703`).
- [x] **T9** — i18n sweep: grep every template and every `.py` under `src/admin_errors/` for
  user-visible literals not wrapped in `{% translate %}`/`{% blocktranslate %}`/`gettext(_lazy)`,
  including the new notification labels, the copy-button labels and any admin `message_user` string;
  wrap what is missed. Result: nothing missed — templates already 100% `{% translate %}`-wrapped
  (Phase 8), the new copy-as-text labels landed wrapped, `notifications.py`'s subject/body labels use
  `gettext()`, and the two `message_user`/`messages.success` strings already used `_()`. System-check
  messages (`checks.py`, including the new `W003`) stay untranslated by existing convention
  (developer-facing, not admin UI — `W001`/`W002`/`E001`/`E002` were never wrapped either).
- [x] **T10** — catalogue: `uv run python -m django makemessages -l uk --settings=tests.settings` from
  `src/admin_errors/`, translate every entry into Ukrainian, `compilemessages`, commit
  `locale/uk/LC_MESSAGES/django.po` + `django.mo`. Re-run `makemessages` and confirm the diff is empty
  (no new/obsolete entries). 95 msgids translated (no plural forms exist — no `ngettext` in the
  codebase); re-run of `makemessages` produced a byte-identical `.po` (verified via `diff`).
- [x] **T11** — i18n tests in `tests/test_admin.py`: a `.po` parser asserting every `msgid` (including
  plurals) has a non-empty `msgstr`; `translation.activate("uk")` makes a known UI string differ from
  its English source (proves the `.mo` loads); the issue list, issue detail and event detail all render
  `200` under `override_settings(LANGUAGE_CODE="uk")`.
- [x] **T12** — e2e additions (`e2e/plans/notifications-i18n.plan.yaml` + `e2e/test_notifications_i18n.py`):
  resolve an issue, re-trigger it, and assert the regressed badge plus a fresh notification line in the
  demo server log (console backend); click "Copy as text" and assert the button label flips to
  *Copied* (clipboard permission granted to the context). Keep it to these two cases — the browser is
  not a second copy of `test_admin.py`. Verified green both inside the full `e2e` run and standalone,
  from a fresh `make e2e-up` each time (see `.autodev/DECISIONS.md` p09 entries for the fingerprint
  and admission-bucket findings that shaped the final test).
- [x] **T13** — `CHANGELOG.md` *Unreleased* entry (notifications, `NOTIFY_BASE_URL`, `W003`, copy-as-text,
  Ukrainian catalogue), then the full gate: `uv run pytest -q`, the four-command lint gate, and
  `make e2e-up && uv run --extra e2e pytest e2e -q && make e2e-down` leaving port 8000 free. All green:
  338 passed/8 skipped, 91% coverage (`--cov-report=term-missing`), ruff+format+django check+
  makemigrations --check all clean, `makemessages -l uk` byte-identical diff, e2e 19 passed, port
  8000 confirmed free after `make e2e-down`.

## Verification

```bash
uv run pytest -q
uv run pytest -q --cov=admin_errors --cov-report=term-missing          # coverage must stay ≥ 90 %
uv run ruff check . && uv run ruff format --check . \
  && uv run python -m django check --settings=tests.settings \
  && uv run python -m django makemigrations admin_errors --check --dry-run --settings=tests.settings
cd src/admin_errors && uv run python -m django makemessages -l uk --settings=tests.settings && git diff --stat
make e2e-up && uv run --extra e2e pytest e2e -q; make e2e-down
```

| Acceptance criterion | Proved by |
|---|---|
| `pytest -q` green | the gate itself (T13) |
| New issue → exactly one email to `ADMINS`, expected subject and body fields | `test_notifications.py::test_new_issue_sends_one_email_to_admins` (+ the body-field assertions) — T4 |
| Regression → one email | `test_notifications.py::test_regression_sends_one_email` — T4 |
| Ignored issue → none | `test_notifications.py::test_ignored_issue_sends_no_email` — T4 |
| Second occurrence inside `NOTIFY_THROTTLE_SECONDS` → none; after the window → one | `test_notifications.py::test_throttle_suppresses_second_notification` / `::test_notification_after_throttle_window` — T4 |
| Two threads notifying concurrently → exactly one email (`transaction=True`) | `test_notifications.py::test_two_threads_notifying_send_one_email` — T5 |
| `NOTIFY_BACKEND=None` disables; a custom dotted path is loaded and used | `test_notifications.py::test_notify_backend_none_disables` / `::test_custom_notify_backend_is_used` — T4 |
| `makemessages -l uk` produces no new untranslated strings; every `msgid` has a non-empty `msgstr` | `test_admin.py::test_ukrainian_catalogue_is_complete` (asserted in-suite) + the manual `makemessages` + empty-diff check — T10/T11 |
| Admin pages render under `LANGUAGE_CODE="uk"` | `test_admin.py::test_pages_render_in_ukrainian` — T11 |
| Copy-as-text present with `view_issue_context`, no locals with `view_issue` only | `test_admin.py::test_copy_as_text_contains_locals_with_context_permission` / `::test_copy_as_text_has_no_locals_without_context_permission` — T7 |
| `mail_admins` handler + `EmailNotifier` → `W003` | `test_settings_and_checks.py::test_w003_mail_admins_overlap` (+ three negative cases) — T6 |
| `NOTIFY_BASE_URL` exists with its default | `test_settings_and_checks.py::test_notify_base_url_default` — T1 |

`assertNumQueries` bounds in `test_storage.py` (15/7/8) and `test_admin.py` (12/15) stay **unchanged**
— the dynamic connect/disconnect design exists precisely so they do not move.

## Risks

| Row | Touched how | What this plan does |
|---|---|---|
| **#13 Email notifications go wrong** (this phase closes it) | The whole notifier | Throttle is a single conditional `UPDATE` executed *before* the send, so only the worker that touched a row sends — proved by the two-thread test (T5). The notifier runs from the existing post-commit `on_commit` hook, so the row is durable first, and `send_safely` swallows and counts a hanging/raising SMTP backend instead of taking the writer down. No retry, by decision. |
| **#2 Secret / PII leak through an ungated surface** | "Copy as text" is a *new* rendering of the payload — exactly the shape of the leak this row names | Double gate: the text is built from the already-`_redact_payload`-ed payload **and** `include_locals=can_view_context`. Tests assert the absence of `SECRET_LOCAL` for a `view_issue`-only user on both the detail and the event-detail page. |
| **#1 We build the wrong product** | The "resolve → hit again → regressed badge + console email" line of the §13 manual QA script only becomes true now | T12's e2e case drives exactly that flow in the demo (console backend, `ADMINS` already configured). |
| **#3 Capture must never raise / recurse** | A new receiver runs inside the capture/writer path | Dispatch stays inside `send_safely`, which holds the recursion guard and isolates receivers; `send_mail` is never called on the request thread's hot path, only post-commit. |
| **#9 N+1 / query budgets** | A connected receiver makes `storage._fire` re-read the issue row | Receivers are connected only when a notification could actually be sent, so the default test host adds zero queries; the budgets are re-run unchanged, never relaxed. |
| **#17 Packaging defect (missing locale in the wheel)** | `locale/` appears for the first time | `pyproject.toml` already declares `locale/**/*.mo`; the `.mo` is committed, and the `package` tox env (wheel install + `django-admin check`) is the standing proof. |
| **#15 Scope creep** | "Also Slack / webhook / templated HTML mail?" | `NOTIFY_BACKEND` is the sanctioned extension point; only `EmailNotifier` and plain text ship. |
| **#16 No loosened checks** | Query budgets and asset-size bounds both sit near new code | Neither bound is edited; if a bound is hit, the product changes. |

## Out of scope

- README sections (notifier setup, replacing Django's `mail_admins` handler, queued email backends,
  FAQ) and the user docs — Phase 10.
- Any locale other than `uk`; `LocaleMiddleware` or a language switcher in the demo.
- HTML / multipart email, digests, per-issue mute, rate-limit-by-recipient, Slack/Telegram/webhook
  backends (spec §2 non-goals).
- Retrying a failed send, an outbox table, or a Celery task for mail delivery.
- The migration squash, screenshots refresh and the final PostgreSQL pass — Phase 10.
- Any change to the fingerprint algorithm, payload schema or models (no migration ships here).
