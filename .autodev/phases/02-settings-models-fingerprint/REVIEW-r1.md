# Review — phase 2 round 1

**Verdict:** changes_requested

Phase 2 lands cleanly against its goal: the settings proxy, the three models, the single initial migration and the frozen fingerprint are all present, all gate commands pass on a fresh run (57 tests, ruff, django check, makemigrations --check all green), every PLAN task is [x] with no environment-blamed [~], and each acceptance criterion maps to a real test — the golden hashes, both normalization families, two-culprits, two-types, message-template keying, the fingerprint= override, the view_issue_context permission after migrate, and the override_settings visible-and-reverts round trip. The frozen-order deviation from spec §8.1's rule listing (ISO-8601 before hex/addr, truncate last) is justified and correct: I checked the quoted-vs-digits reorder is output-equivalent, and ISO-before-digits is load-bearing. Two majors hold it back. First, nothing tests that the system checks are actually registered from ready(): both targeted tests call the check functions directly, and the one run_checks() test asserts an empty list, which is also what an unregistered check produces — delete both register() calls and the suite stays green while W001 silently stops reaching hosts. Second, Issue.resolved_by has no related_name, so admin_errors claims User.issue_set; any host project with its own Issue model FK'd to the user model hits fields.E304 and cannot start, and the field signature is baked into the frozen initial migration. The remaining findings are small: a tautological second assertion in the golden parts-string test, an index test that proves only query count, missing Meta verbose_name across all three models, an off-by-one "38 keys" in the CHANGELOG (spec has 39; the key sets themselves match exactly), and one unpinned normalization precedence.

## [MAJOR] No test proves the checks are actually registered from ready()
`tests/test_settings_and_checks.py`

`apps.py:20-21` registering W001/E002 is the phase deliverable and the risk-#12 mitigation, but nothing in the suite can fail if that registration is deleted. `test_unknown_setting_key_raises_w001` and `test_old_sqlite_raises_e002` call `checks.check_settings_keys()` / `checks.check_sqlite_version()` directly, bypassing the registry entirely. The only test that goes through the registry, `test_default_test_settings_produce_no_admin_errors_messages`, asserts the admin_errors message list is *empty* — which is exactly the result you get when the checks were never registered. So the wiring in `ready()` is untested: remove both `register()` calls and all 57 tests still pass, while a host's `manage.py check` silently stops warning about typo'd `ADMIN_ERRORS` keys. I confirmed registration currently works (`run_checks()` under `override_settings(ADMIN_ERRORS={"NOPE": 1})` returns `['admin_errors.W001']`), so this is purely a missing regression guard on a deliberately fragile bit of wiring.

**Fix:** In `test_unknown_setting_key_raises_w001`, go through the registry instead of (or in addition to) the direct call:

```python
with override_settings(ADMIN_ERRORS={"NOPE": 1}):
    ids = [m.id for m in run_checks()]
assert ids.count(checks.W001_ID) == 1
```

That one change makes both the check body and the `ready()` registration load-bearing, and keeps `test_default_test_settings_produce_no_admin_errors_messages` meaningful as its negative counterpart.

## [MAJOR] Issue.resolved_by has no related_name; reverse accessor User.issue_set can break a host project
`src/admin_errors/models.py`

`Issue.resolved_by` (models.py:30-36) is an FK to `AUTH_USER_MODEL` with no `related_name`, so it claims the default reverse accessor `User.issue_set` and the default reverse query name `issue`. For a reusable app installed into arbitrary projects this is a live collision: any host with its own model named `Issue` pointing at the user model without an explicit `related_name` (an issue tracker, a support-ticket app, a QA app — plausible in exactly the kind of project that installs an error tracker) gets `fields.E304 Reverse accessor 'User.issue_set' for 'admin_errors.Issue.resolved_by' clashes with reverse accessor for '<other>.Issue.<field>'`. That is a system-check Error: the host's `manage.py check`, `migrate` and `runserver` all refuse to start, and the only remedy is editing the *other* app. Every other FK in this phase (`Event.issue`, `IssueDailyCount.issue`) does set `related_name`, so this is an omission rather than a considered choice — spec §6.1 writes the field as `FK(AUTH_USER_MODEL, null, SET_NULL)` but does not forbid a related_name, and PLAN.md/DECISIONS.md do not discuss it. Cost rises once 0.1.0 ships, since `related_name` is part of the field's `deconstruct()` and the initial migration is a frozen contract.

**Fix:** Add an explicit reverse name to `Issue.resolved_by` and regenerate `0001_initial.py` (the phase already owns the single migration, so this stays one file):

```python
resolved_by = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    verbose_name=_("resolved by"),
    null=True, blank=True,
    on_delete=models.SET_NULL,
    related_name="+",          # or "resolved_admin_errors_issues" if a reverse is wanted later
)
```

Nothing in the spec needs the reverse side, so `related_name="+"` is the smaller contract. Worth a line in DECISIONS.md since it deviates from the literal spec §6.1 field signature.

## [MINOR] Second assertion in the golden parts-string test is tautological
`tests/test_fingerprint.py`

`test_golden_exception_fingerprint_matches_the_documented_parts_string` (lines 26-33) ends with `fp.for_exception(...) == fp.compute(joined.split(":"))`. `joined` is `":".join(GOLDEN_EXCEPTION_PARTS)`, and `compute()` re-joins its argument with `":"`, so `compute(joined.split(":"))` hashes byte-for-byte the same string as `compute(GOLDEN_EXCEPTION_PARTS)` no matter how the parts were split — the split/rejoin round trip cannot change the result. (The golden message itself contains a `":"`, so the two lists genuinely differ and the equality still holds.) The left-hand side is already pinned by `test_golden_exception_fingerprint` on line 23, so this assertion adds no coverage while reading like it verifies the parts layout.

**Fix:** Assert the readable intermediate directly instead of round-tripping:

```python
assert fp.qualified_type_name(ValueError) == GOLDEN_EXCEPTION_PARTS[0]
assert fp.normalize_message("invalid literal for int() with base 10: 'abc'") == GOLDEN_EXCEPTION_PARTS[2]
assert fp.compute(GOLDEN_EXCEPTION_PARTS) == GOLDEN_EXCEPTION_HASH
```

That pins each part independently, so a normalization change fails with a readable diff rather than only via the hash.

## [MINOR] test_event_index_enforced_by_query proves nothing about the index
`tests/test_models.py`

`test_event_index_enforced_by_query` (lines 76-80) creates one event and asserts `list(issue.events.all())` runs in one query. A single-query count is true with or without `ae_event_issue_ts_idx` — no index exists that could change it — so the test name overstates what it checks and PLAN.md T7's promise that the indexes are "enforced by the database" is not met (only the unique constraints are, via the two `IntegrityError` tests, which are correctly wrapped in their own `transaction.atomic()` for PG). Not a correctness bug, but a test whose name will mislead the next reader into believing index coverage exists.

**Fix:** Either rename it to something honest (`test_events_related_name_fetches_in_one_query`), or make it actually assert the index reached the schema, e.g. via the introspection the migration produced:

```python
from django.db import connection
with connection.cursor() as cursor:
    names = {i.name for i in connection.introspection.get_constraints(cursor, Event._meta.db_table).values() if i.get("index")}
```

(or compare `connection.introspection.get_constraints(...)` keys against the four explicit names). The introspection form also guards the explicit-name decision in DECISIONS.md, which is otherwise only asserted at the `Meta` level.

## [MINOR] Model Meta lacks verbose_name/verbose_name_plural, and two FK fields lack verbose_name
`src/admin_errors/models.py`

Every scalar field carries a `gettext_lazy` `verbose_name`, but no model declares `Meta.verbose_name` / `verbose_name_plural`, and `Event.issue` (line 54) and `IssueDailyCount.issue` (line 68) carry none either. The admin index under the translated app label "Errors" will therefore show untranslatable English model names ("Issues", "Events", "Issue daily counts") and an untranslated "issue" column label, which the Ukrainian catalogue in Phase 9 cannot reach. Both `Meta.verbose_name` and a field's `verbose_name` are part of the migration state, so fixing this after 0.1.0 means an `AlterModelOptions`/`AlterField` migration against the one-migration rule — cheap now, since this phase owns `0001_initial.py`, and still recoverable at the Phase 10 squash, but the whole point of the phase is pinning the schema contract.

**Fix:** Add to each model's `Meta`, e.g. for `Issue`:

```python
verbose_name = _("issue")
verbose_name_plural = _("issues")
```

and give `Event.issue` / `IssueDailyCount.issue` a `verbose_name=_("issue")`, then regenerate `0001_initial.py`. Phase 8/9 own the templates and the catalogue; the model-layer strings belong here with the rest of them.

## [NIT] "38 spec-defined keys" is off by one — there are 39
`CHANGELOG.md`

The CHANGELOG *Unreleased* entry, PLAN.md line 48 and the `p02-plan/conf` DECISIONS bullet all say the proxy holds "all 38 spec-defined keys". Spec §5's table has 39 rows and `DEFAULTS` has 39 entries (parsed both and diffed the key sets — empty in both directions, so the content is correct; only the count is wrong). Minor, but the CHANGELOG is user-facing and the number reads like a checkable invariant.

**Fix:** Say 39 in CHANGELOG.md, or drop the count and write "every key in spec §5" — `test_defaults_match_spec_section_5` is the real invariant and does not depend on a number staying in sync.

## [NIT] Hex-beats-digits precedence for long digit runs is frozen but unpinned
`tests/test_fingerprint.py`

`HEX_RE = \b[0-9a-fA-F]{8,}\b` runs before `DIGITS_RE`, so a purely numeric run of 8+ digits normalizes to `<hex>`, not `#` — `"Order 12345678 failed"` → `"Order <hex> failed"`. That follows spec §8.1's ordering ("hex strings ≥ 8 chars" before "any remaining digit run"), so it is intended, but it is a surprising frozen behaviour and the parametrized family table only covers a 5-digit case (line 49) and an alphabetic hex case (line 51). A later well-meaning "fix" to the digit rule would not be caught.

**Fix:** Add one row to `test_normalizes_each_family`:

```python
("Order 12345678 failed", "Order <hex> failed"),
```

and a short comment noting the ordering is deliberate per spec §8.1 / ADR 0003.
