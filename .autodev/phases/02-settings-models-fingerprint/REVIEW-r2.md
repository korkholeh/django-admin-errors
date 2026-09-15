# Review — phase 2 round 2

**Verdict:** changes_requested

Phase 2 hits its goal: the settings proxy, the three models, exactly one initial migration and the frozen fingerprint are all present, and every acceptance criterion maps to a real test that can fail. I re-verified independently: 59 tests pass, `django check` and `makemigrations --check --dry-run` exit 0, `migrations/` holds only `0001_initial.py`, the three golden hashes match stdlib `sha1` of the documented parts strings, and `DEFAULTS` matches all 39 rows of spec §5 key-for-key and value-for-value. Both r1 majors are really fixed: W001/E002 tests now go through `run_checks()` so the `ready()` registration is load-bearing, and `Issue.resolved_by` carries `related_name="+"` in both model and regenerated migration. All r1 minors/nits are addressed too (parts pinned individually, introspection test for the four explicit index/constraint names, `Meta.verbose_name` everywhere, CHANGELOG 39, hex-beats-digits row). No deviation from ARCHITECTURE.md or ADR 0003/0008, all 10 PLAN tasks `[x]`, no `[~]`, no environment blame. Two majors hold it back. First, the documented lint gate is red on the branch: ruff formats Python fences inside Markdown and has no `exclude`, so `ruff format --check .` fails on `.autodev/phases/02-.../REVIEW-r1.md` — the CI `lint` job fails and will keep failing as every future review doc lands; the implement step recorded this as "pre-existing, not ours" but the fix is one line in the pyproject.toml this phase already edits. Second, `normalize_message` returns `""` for any message whose first physical line is blank, so distinct multi-line messages raised at the same site collapse into one empty-titled issue — cheap to fix now, a major-version breaking change once the hash is frozen. Remaining findings are small: a dead `as_dict()`, one untested E002 skip branch, one redundant coercion.

## [MAJOR] Lint gate is red on the branch: ruff formats Python fences in .autodev review docs and has no exclude
`pyproject.toml`

The CLAUDE.md lint command and the CI `lint` tox env both run `ruff format --check .` over the whole repository. Verified on the current tree: `uv run ruff check .` exits 0 ("All checks passed!") but `uv run ruff format --check .` exits 1 with `unformatted: File would be reformatted --> .autodev/phases/02-settings-models-fingerprint/REVIEW-r1.md:33:15` ("1 file would be reformatted, 44 files already formatted"). ruff 0.16 formats Python code blocks inside Markdown by default, and `[tool.ruff]` in pyproject.toml declares no `exclude`/`extend-exclude` — the `exclude = [".autodev/", "demo/", "e2e/", "docs/img/"]` at pyproject.toml:68 belongs to `[tool.hatch.build.targets.sdist]` and has no effect on ruff, exactly as the `p02-review_fix1/rejected` decision states. Consequences: the `lint` job in `.github/workflows/ci.yml` (which runs `tox -e lint` → `ruff check .` then `ruff format --check .`) is red on this branch; CLAUDE.md's "Every phase ends with ruff clean" is violated as committed; and the problem compounds — every future REVIEW-r*.md with a Python fence re-breaks it. Worse, the documented `format` command (`uv run ruff format .`) would silently rewrite the reviewer's quoted snippets inside an orchestrator-owned artifact. The implement step was right not to edit REVIEW-r1.md, but the conclusion ("not ours, leave it red") leaves a documented gate failing; `.autodev/` is run bookkeeping, not source, so excluding it from the formatter is the correct scope fix, not a weakened check.

**Fix:** Add to `[tool.ruff]` in pyproject.toml:

```toml
extend-exclude = [".autodev"]
```

Then re-run `uv run ruff check . && uv run ruff format --check .` (exits 0) and log the decision in `.autodev/DECISIONS.md`. Source, tests, docs/ and e2e/ stay formatted; only the orchestrator's own run log is out of the formatter's reach.

## [MAJOR] normalize_message returns "" when the first physical line is blank, merging distinct messages in a frozen contract
`src/admin_errors/fingerprint.py`

`normalize_message` (fingerprint.py:33) takes `message.splitlines()[0]` before stripping, so any message whose first physical line is empty or whitespace normalizes to the empty string. Verified against the real module: `normalize_message('\n  Configuration invalid: missing KEY\n  fix it')` -> `''`, and `for_exception(ValueError, 'a.b', '\nfoo') == for_exception(ValueError, 'a.b', '\nbar')` -> `True`. Every such exception with the same type and culprit therefore collapses into one issue whose normalized message part is empty, which is exactly the under-differentiation ADR 0003 exists to prevent — and it is invisible in the golden tests because `test_normalize_message_keeps_only_the_first_line` only covers a message whose first line is non-empty. The triggering shape is ordinary Python: a `raise ValueError("""\n  multi-line explanation ...""")` or any dedented triple-quoted error text starts with a newline. Blast radius is bounded (same type + same culprit, so usually the same raise site) but the payload is a frozen public contract: per ADR 0003 and the module docstring, changing the normalization after 0.1.0 needs a major version bump and a migration note, so this is the last cheap moment to fix it. Taking the first non-blank line is a refinement of spec §8.1's "first line", not a deviation from it: a leading blank line is not the message.

**Fix:** In `normalize_message`, pick the first line that has content before running the rules, e.g.

```python
text = next((line.strip() for line in message.splitlines() if line.strip()), "")
```

(keeps `normalize_message("") == ""`, leaves all existing goldens unchanged — the three pinned hashes have non-blank first lines, verified). Add a parametrized row `("\n  Configuration invalid: missing KEY\n  fix it", "Configuration invalid: missing KEY")` to `test_normalizes_each_family`, plus an assertion that two different leading-newline messages at the same culprit do not share a fingerprint, and record the choice in DECISIONS.md next to the existing `p02-plan/fingerprint` order entry.

## [MINOR] Settings.as_dict() has no caller and no test
`src/admin_errors/conf.py`

`as_dict()` (conf.py:79-80) is the only uncovered line in conf.py (`--cov-report=term-missing` reports `conf.py ... 97% Missing: 80`). Grepping the whole tree for `as_dict` finds exactly one hit: the definition itself. PLAN.md's design block justifies it as the "merged view, used by tests", but no test uses it, and nothing in this phase's scope consumes it — it is a public method on the settings proxy that nobody exercises, so a regression in it (for instance forgetting the `dict(...)` copy and handing callers the live cache) would go unnoticed.

**Fix:** Either delete `as_dict()` until a consumer exists, or give it the one test the plan implies: `assert settings.as_dict() == {**DEFAULTS, "TRANSPORT": "sync"}` and `assert settings.as_dict() is not settings._resolved()` (the copy is the point — a caller mutating the returned dict must not poison the cache).

## [MINOR] E002's skip path (alias absent or non-SQLite engine) is untested on the SQLite gate
`src/admin_errors/checks.py`

`check_sqlite_version` returns `[]` at checks.py:33 when the configured `DATABASE` alias is missing from `DATABASES` or its `ENGINE` is not SQLite. That line is the only uncovered statement in checks.py (`checks.py ... 90% Missing: 33`) on the SQLite gate: `test_old_sqlite_raises_e002` reaches it only via its `else` branch, which runs solely under `DJANGO_DB=postgres`. The branch is load-bearing twice over — it is what keeps E002 quiet for hosts on the dedicated-alias setup of spec §11.3, and PLAN.md explicitly delegates the missing-alias case to E001 in Phase 3, so a future edit here must not start emitting E002 (or raising) for an alias that does not exist. I confirmed the behaviour is currently correct (`override_settings(ADMIN_ERRORS={'DATABASE': 'errors'})` -> `check_sqlite_version()` returns `[]`), so this is a missing regression guard rather than a bug.

**Fix:** Add to tests/test_settings_and_checks.py, backend-independent so it is meaningful on both gates:

```python
def test_e002_is_skipped_for_an_unknown_database_alias(monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 8, 3))
    with override_settings(ADMIN_ERRORS={"DATABASE": "errors"}):
        assert [m for m in run_checks() if m.id == checks.E002_ID] == []
```

## [NIT] for_override annotates value as str but re-coerces it with str()
`src/admin_errors/fingerprint.py`

`for_override(value: str) -> str` (fingerprint.py:63-64) returns `compute([str(value)])`. If the annotation is the contract, the `str()` is dead; if non-`str` overrides are really expected from `extra={"fingerprint": ...}` (where a host can put anything), the annotation is wrong and the coercion is the contract. Since this module is frozen, the ambiguity is worth resolving in the same breath as the rest of it — note that `for_message` already models the deliberate version of this (`template: object` plus an `isinstance` branch).

**Fix:** Pick one: `def for_override(value: object) -> str: return compute([str(value)])` (keeps hostile input safe, matches `for_message`'s shape), or keep `value: str` and drop the `str()`. The first is the safer default for a value that arrives from a host's `extra` dict.
