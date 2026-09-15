"""Stdlib-only checks that pin tox.ini / ci.yml wiring (ADR-less, plain text/config parsing).

No PyYAML dependency: ci.yml's `matrix.include` block is scraped with a regex instead.
"""

import configparser
import re

PROFILE_SQLITE_CELLS = [
    ("310", "42"),
    ("310", "52"),
    ("311", "42"),
    ("311", "52"),
    ("312", "42"),
    ("312", "52"),
    ("312", "60"),
    ("312", "61"),
    ("313", "52"),
    ("313", "60"),
    ("313", "61"),
]


def test_tox_env_list_matches_profile_matrix(repo_root) -> None:
    parser = configparser.ConfigParser()
    parser.read(repo_root / "tox.ini")
    env_list = [line.strip() for line in parser.get("tox", "env_list").splitlines() if line.strip()]
    assert env_list == [
        "py{310,311}-dj{42,52}-sqlite",
        "py312-dj{42,52,60,61}-sqlite",
        "py313-dj{52,60,61}-sqlite",
        "lint",
        "py313-dj{52,61}-postgres",
        "celery",
        "package",
    ]


def test_tox_postgres_envs_are_not_shadowed_by_a_generic_sqlite_section(repo_root) -> None:
    text = (repo_root / "tox.ini").read_text()
    # This brace pattern is exactly the bug from review r1: a generic section whose
    # name expands to both the sqlite and postgres env names, matched before the
    # dedicated postgres section (tox 4 uses the first matching section).
    assert "{sqlite,postgres}" not in text
    for section in ("testenv:py313-dj52-postgres", "testenv:py313-dj61-postgres"):
        assert f"[{section}]" in text, f"missing dedicated section [{section}]"


def test_tox_postgres_envs_set_django_db_and_postgres_extra(repo_root) -> None:
    parser = configparser.ConfigParser()
    parser.read(repo_root / "tox.ini")
    for section in ("testenv:py313-dj52-postgres", "testenv:py313-dj61-postgres"):
        assert parser.has_section(section), section
        assert "DJANGO_DB = postgres" in parser.get(section, "set_env")
        assert "postgres" in parser.get(section, "extras")


def test_tox_package_env_installs_from_find_links_not_a_literal_glob(repo_root) -> None:
    text = (repo_root / "tox.ini").read_text()
    # `dist/*.whl` is passed literally by tox (no shell), so it must never appear as
    # an install target; --find-links lets uv/pip resolve the built artifact instead.
    assert "dist/*.whl" not in text
    assert "--find-links dist" in text


def _expand_tox_factors(pattern: str) -> list[str]:
    """Expand one tox brace-factor group (`py{310,311}-dj42-sqlite`) into concrete env names."""
    match = re.search(r"\{([^}]+)\}", pattern)
    if not match:
        return [pattern]
    expanded = []
    for option in match.group(1).split(","):
        candidate = pattern[: match.start()] + option + pattern[match.end() :]
        expanded.extend(_expand_tox_factors(candidate))
    return expanded


def test_ci_matrix_lists_exactly_the_profile_sqlite_cells(repo_root) -> None:
    # The env names ci.yml passes to `tox -e` must be the *exact* strings tox.ini resolves to
    # (dotless, e.g. "py310-dj42-sqlite") — tox rejects anything else outright (review r2 blocker).
    parser = configparser.ConfigParser()
    parser.read(repo_root / "tox.ini")
    env_list = [line.strip() for line in parser.get("tox", "env_list").splitlines() if line.strip()]
    sqlite_patterns = [line for line in env_list if line.endswith("-sqlite")]
    expected_envs = [env for pattern in sqlite_patterns for env in _expand_tox_factors(pattern)]
    assert expected_envs == [f"py{p}-dj{d}-sqlite" for p, d in PROFILE_SQLITE_CELLS]

    text = (repo_root / ".github" / "workflows" / "ci.yml").read_text()
    match = re.search(r"matrix:\n(?:\s+include:\n)((?:\s+-\s*\{.*\}\n)+)", text)
    assert match, "could not find matrix.include block in ci.yml"
    toxenvs = re.findall(r'toxenv:\s*"([a-z0-9-]+)"', match.group(1))
    assert toxenvs == expected_envs

    run_step = text.split("sqlite-matrix:", 1)[1]
    assert "matrix.toxenv" in run_step
    assert "matrix.python" not in run_step
    assert "matrix.django" not in run_step


def test_ci_defines_the_five_jobs(repo_root) -> None:
    text = (repo_root / ".github" / "workflows" / "ci.yml").read_text()
    jobs_block = text.split("\njobs:\n", 1)[1]
    job_names = re.findall(r"^  ([a-z][a-z0-9-]*):\n", jobs_block, re.MULTILINE)
    assert job_names == ["lint", "sqlite-matrix", "postgres", "celery", "package"]
