"""Release/doc invariant tests (spec section 18, phase 10).

Pure-file tests: no DB, no Django app registry dependency beyond what `conf` needs (it only reads
`django.conf.settings`, already configured by `tests/settings.py`). These pin claims made in
`README.md` / `CHANGELOG.md` / packaging metadata against the actual code, so a stale doc fails the
suite instead of only being caught by a human re-reading the README.
"""

from __future__ import annotations

import importlib.metadata
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10 has no tomllib; `tomli` is its backport, same API.
    import tomli as tomllib

import admin_errors
from admin_errors.conf import DEFAULTS

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def _readme_text() -> str:
    return README.read_text(encoding="utf-8")


def _changelog_text() -> str:
    return CHANGELOG.read_text(encoding="utf-8")


def _settings_table_rows(readme_text: str) -> list[list[str]]:
    """Extract the `| Key | Default | Meaning |` table's data rows (key, default, meaning)."""
    lines = readme_text.splitlines()
    rows: list[list[str]] = []
    in_table = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("| `ENABLED`"):
            in_table = True
        if not in_table:
            continue
        if not stripped.startswith("|"):
            break
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2:
            break
        rows.append(cells)
    return rows


def test_readme_settings_table_covers_every_setting() -> None:
    text = _readme_text()
    rows = _settings_table_rows(text)
    assert rows, "settings table not found in README.md"
    documented = {row[0].strip("`") for row in rows}
    assert documented == set(DEFAULTS), (
        f"missing from README: {set(DEFAULTS) - documented}; "
        f"extra/stale in README: {documented - set(DEFAULTS)}"
    )


def _normalize_default(value: object) -> str:
    if value is None:
        return "`None`"
    if isinstance(value, bool):
        return f"`{value}`"
    if isinstance(value, str):
        return f'`"{value}"`'
    if isinstance(value, list):
        return "`" + repr(value).replace("'", '"') + "`"
    return f"`{value!r}`"


def test_readme_settings_table_defaults_match_conf() -> None:
    text = _readme_text()
    rows = _settings_table_rows(text)
    by_key = {row[0].strip("`"): row[1].strip() for row in rows}
    mismatches = []
    for key, expected in DEFAULTS.items():
        documented = by_key.get(key, "")
        normalized_expected = _normalize_default(expected)
        # Exact equality on the whole Default cell: it holds nothing but the backticked value
        # (e.g. "`True`", "`[\"a\", \"b\"]`"), so a stale default like a documented `500` for an
        # expected `50` (or `36000` for `3600`) must fail rather than pass on a substring match.
        if documented != normalized_expected:
            mismatches.append((key, expected, documented))
    assert not mismatches, f"README default mismatches conf.DEFAULTS: {mismatches}"


def test_readme_embeds_every_committed_screenshot() -> None:
    text = _readme_text()
    img_dir = REPO_ROOT / "docs" / "img"
    committed = {p.name for p in img_dir.glob("*.png")}
    assert committed, "no screenshots found under docs/img/"
    referenced = set(re.findall(r"docs/img/([\w.-]+\.png)", text))
    assert committed <= referenced, f"screenshots not embedded in README: {committed - referenced}"
    assert referenced <= committed, (
        f"README references missing screenshots: {referenced - committed}"
    )


def test_readme_has_the_spec_17_sections() -> None:
    raw_text = _readme_text()
    headings = "\n".join(re.findall(r"^#{2,3} .*$", raw_text, re.MULTILINE)).lower()
    required_fragments = [
        "screenshot",
        "install",
        "how it works",
        "settings",
        "permission",
        "retention",
        "notification",
        "celery",
        "storage bound",
        "faq",
        "non-goal",
        "contribut",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in headings]
    assert not missing, f"README is missing sections for: {missing}"
    assert "```" in raw_text, "no fenced code/diagram block found in README"
    # Bound the slice to the section itself (next "## " heading), not everything after it: later
    # sections (Install snippets, Celery beat, FAQ) also contain fences, so an unbounded slice
    # could never fail even if "How it works" itself lost its diagram.
    heading = re.search(r"^## .*How it works.*$", raw_text, re.MULTILINE)
    assert heading, "no 'How it works' section found in README"
    next_heading = re.search(r"^## ", raw_text[heading.end() :], re.MULTILINE)
    section_end = heading.end() + next_heading.start() if next_heading else len(raw_text)
    how_it_works = raw_text[heading.end() : section_end]
    assert "```" in how_it_works, "no fenced ASCII diagram under 'How it works'"
    assert "writer thread" in how_it_works, "'How it works' diagram missing the writer thread step"


def test_readme_faq_maps_each_silent_failure_to_an_instrument() -> None:
    raw_text = _readme_text()
    # Bound the slice to the FAQ section itself (next "## " heading), not everything after the
    # first literal "FAQ": that string also appears in the Install section's "(see the FAQ below)"
    # cross-reference, so an unbounded split would pass even with the whole FAQ section deleted.
    heading = re.search(r"^## .*FAQ.*$", raw_text, re.MULTILINE)
    assert heading, "no 'FAQ' section found in README"
    next_heading = re.search(r"^## ", raw_text[heading.end() :], re.MULTILINE)
    section_end = heading.end() + next_heading.start() if next_heading else len(raw_text)
    faq_section = raw_text[heading.end() : section_end]
    required_terms = [
        "propagate",
        "DEBUG",
        "CAPTURE_LEVEL",
        "errors_test",
        "errors_stats",
        "NEW_ISSUES_PER_MINUTE",
        "EVENT_SAMPLE_PER_HOUR",
        "view_issue_context",
    ]
    missing = [term for term in required_terms if term not in faq_section]
    assert not missing, f"README FAQ is missing references to: {missing}"


def test_changelog_has_a_released_section_for_the_current_version() -> None:
    version = admin_errors.__version__
    text = _changelog_text()
    match = re.search(
        rf"^## \[{re.escape(version)}\] .* \d{{4}}-\d{{2}}-\d{{2}}", text, re.MULTILINE
    )
    assert match, f"CHANGELOG.md has no '## [{version}] - YYYY-MM-DD' heading"
    unreleased_match = re.search(
        r"^## \[Unreleased\]\s*\n(.*?)(?=^## \[)", text, re.MULTILINE | re.DOTALL
    )
    assert unreleased_match, "CHANGELOG.md has no [Unreleased] section above the release"
    leftover = unreleased_match.group(1).strip()
    ok = leftover == "" or len(leftover.splitlines()) <= 2
    assert ok, (
        f"[Unreleased] should be a placeholder once {version} is released, found: {leftover!r}"
    )


def test_changelog_0_1_0_records_benchmark_numbers() -> None:
    text = _changelog_text()
    match = re.search(r"^## \[0\.1\.0\].*?(?=^## \[|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, "no [0.1.0] section found"
    section = match.group(0)
    for label in ("no locals", "with locals", "count-only"):
        assert label in section, f"benchmark row {label!r} missing from CHANGELOG 0.1.0 section"
    assert re.search(r"\d+(\.\d+)?\s*ms", section), "no ms figure found in CHANGELOG 0.1.0 section"


def test_version_matches_changelog() -> None:
    """`__version__` is what the release workflow checks the tag against, so the newest CHANGELOG
    heading has to be the same string — derived from `__version__` rather than pinned to a literal,
    so a release bump does not have to edit this test to stay honest."""
    version = admin_errors.__version__
    text = _changelog_text()
    headings = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, re.MULTILINE)
    assert headings, "no version headings found in CHANGELOG.md"
    assert headings[0] == version, (
        f"newest CHANGELOG heading is {headings[0]!r}, expected {version!r}"
    )


def test_django_is_the_only_runtime_dependency() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["dependencies"]
    assert len(deps) == 1
    assert deps[0].split(">=")[0].split("==")[0].strip() == "Django"

    requires = importlib.metadata.requires("django-admin-errors") or []
    non_extra = [req for req in requires if "extra ==" not in req]
    assert len(non_extra) == 1
    assert non_extra[0].split(" ")[0].split(">=")[0].split("(")[0].strip().lower() == "django"


def test_exactly_one_migration_ships() -> None:
    migrations_dir = REPO_ROOT / "src" / "admin_errors" / "migrations"
    numbered = sorted(p.name for p in migrations_dir.glob("[0-9][0-9][0-9][0-9]_*.py"))
    assert numbered == ["0001_initial.py"], f"expected exactly one migration, found: {numbered}"


def test_no_pragma_no_cover_on_capture_or_storage_paths() -> None:
    guarded_modules = ["capture.py", "context.py", "storage.py", "writer.py", "retention.py"]
    src_dir = REPO_ROOT / "src" / "admin_errors"
    offenders = []
    for name in guarded_modules:
        text = (src_dir / name).read_text(encoding="utf-8")
        if "pragma: no cover" in text:
            offenders.append(name)
    assert not offenders, f"'# pragma: no cover' found in guarded modules: {offenders}"


def test_user_docs_index_links_resolve() -> None:
    user_docs_dir = REPO_ROOT / "docs" / "user"
    assert user_docs_dir.is_dir(), "docs/user/ does not exist"
    md_files = sorted(user_docs_dir.glob("*.md"))
    assert md_files, "docs/user/ has no markdown files"
    link_pattern = re.compile(r"\]\(([^)]+\.md)\)")
    for path in md_files:
        text = path.read_text(encoding="utf-8")
        for link in link_pattern.findall(text):
            if link.startswith("http"):
                continue
            target = link.split("#", 1)[0]
            resolved = (path.parent / target).resolve()
            assert resolved.is_file(), f"{path.name} links to missing file {target!r}"
