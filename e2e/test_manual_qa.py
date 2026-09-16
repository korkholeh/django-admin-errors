"""Plan: e2e/plans/manual-qa.plan.yaml. The spec section 13 manual QA script, encoded as a
durable spec instead of a one-night human claim: message-normalization aggregation, storm
sampling and speed, the regressed badge, `errors_cleanup --dry-run`'s report, and dark-mode
readability of the badge/sparkline colours. The regression notification email is covered at unit
level by `tests/test_notifications.py::test_regression_sends_one_email`, not here.
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import pytest

pytest.importorskip("playwright.sync_api")

from e2e.conftest import login, wait_until

ISSUE_LIST_PATH = "/admin/admin_errors/issue/"
SERVER_LOG_PATH = Path("/tmp/admin-errors-e2e-server.log")
REPO_ROOT = Path(__file__).resolve().parent.parent
EVENT_SAMPLE_PER_HOUR = 5  # demo/demo_project/settings.py
RETENTION_LABELS = (
    "events",
    "daily_counts",
    "resolved_issues",
    "ignored_issues",
    "open_issues",
    "evicted_issues",
)


def _list_url(base_url: str, query: str | None = None) -> str:
    url = f"{base_url}{ISSUE_LIST_PATH}"
    return f"{url}?q={query}" if query else url


def _row_count(page, base_url: str, query: str) -> int:
    page.goto(_list_url(base_url, query))
    return page.locator("#result_list tbody tr").count()


def _parse_compact_count(text: str) -> int:
    """Inverse of `ae_compact_number` (admin_errors_tags.py): "3" -> 3, "1.2k" -> 1200."""
    text = text.strip()
    multipliers = {"k": 1_000, "m": 1_000_000}
    suffix = text[-1].lower()
    if suffix in multipliers:
        return round(float(text[:-1]) * multipliers[suffix])
    return int(text)


def _issue_count(page, base_url: str, query: str) -> int:
    """The `Count` column of the single row matching `query` search."""
    page.goto(_list_url(base_url, query))
    rows = page.locator("#result_list tbody tr")
    assert rows.count() == 1, f"expected exactly one row for {query!r}, got {rows.count()}"
    return _parse_compact_count(rows.first.locator("td.field-count_display").inner_text())


def _detail_url(page, base_url: str, query: str) -> str:
    page.goto(_list_url(base_url, query))
    rows = page.locator("#result_list tbody tr")
    assert rows.count() == 1, f"expected exactly one row for {query!r}, got {rows.count()}"
    href = rows.first.locator("a.ae-issue-cell").get_attribute("href")
    return urljoin(f"{base_url}/", href)


def _delete_existing_issue(page, base_url: str, query: str) -> None:
    """Delete any pre-existing issue for `query`, through the admin's own bulk-delete action.

    `make e2e-up` is idempotent and reuses an already-answering demo server, so a retry of this
    suite (or an earlier test file's own hits, per p09-e2e/p10-T11) can leave a stale row behind.
    Deleting it first re-establishes "never hit before in this server run" unconditionally.

    A hit from an *earlier* test (a shared fingerprint like `/boom/`'s `ZeroDivisionError`) can
    still be sitting in the writer thread's queue (`FLUSH_INTERVAL_SECONDS`, default 1.0s) and
    land after this delete, silently recreating the row with a stale count. Retrying the delete
    past one flush interval, and requiring the row to *stay* absent, is what makes "cleared"
    trustworthy rather than a snapshot that a delayed write immediately invalidates.
    """
    for _ in range(5):
        page.goto(_list_url(base_url, query))
        rows = page.locator("#result_list tbody tr")
        if rows.count() > 0:
            page.locator("#action-toggle").check()
            page.locator("select[name=action]").select_option("delete_selected")
            page.locator("button[title='Run the selected action']").click()
            page.locator("input[type=submit]").click()
            assert _row_count(page, base_url, query) == 0, f"failed to clear {query!r}"
        # Wait past one flush interval and recheck even if nothing needed deleting just now: a
        # hit from an earlier test can still be queued and land in this window.
        page.wait_for_timeout(1500)
        if _row_count(page, base_url, query) == 0:
            return
    raise AssertionError(f"issue for {query!r} kept reappearing")


def _server_log_offset() -> int:
    return SERVER_LOG_PATH.stat().st_size if SERVER_LOG_PATH.exists() else 0


def _server_log_contains(*needles: str, since: int = 0) -> bool:
    if not SERVER_LOG_PATH.exists():
        return False
    with SERVER_LOG_PATH.open("rb") as handle:
        handle.seek(since)
        text = handle.read().decode(errors="replace")
    return all(needle in text for needle in needles)


def test_three_boom_hits_make_one_issue_with_count_three(page, base_url, server_available) -> None:
    """[qa:manual-qa:three-boom-hits-one-issue-count-three] Three /boom/ hits aggregate to one
    issue, count 3, and the creation fires a 'New issue:' notification."""
    login(page, base_url, "admin", "admin")
    query = "ZeroDivisionError"
    _delete_existing_issue(page, base_url, query)

    log_offset = _server_log_offset()
    for _ in range(3):
        page.goto(f"{base_url}/boom/")

    wait_until(page, _list_url(base_url, query), lambda p: _row_count(p, base_url, query) >= 1)
    assert _row_count(page, base_url, query) == 1
    assert _issue_count(page, base_url, query) == 3

    deadline = time.monotonic() + 10.0
    while not _server_log_contains("New issue:", query, since=log_offset):
        if time.monotonic() >= deadline:
            raise AssertionError(f"expected a 'New issue:' notification for {query!r}")
        time.sleep(0.5)


def test_boom_1_and_boom_2_group_into_one_issue(page, base_url, server_available) -> None:
    """[qa:manual-qa:boom-1-and-boom-2-group-into-one-issue] /boom/1/ and /boom/2/ collapse into
    one ValueError issue, count 2."""
    login(page, base_url, "admin", "admin")
    # "boom_n" (the culprit), not the message text: `title` is built from the *normalized*
    # message (fingerprint.py, ADR 0003), which strips the numeric argument the raw "bad value
    # {n}" string carries, so searching for "bad value" itself finds nothing.
    query = "boom_n"
    _delete_existing_issue(page, base_url, query)

    page.goto(f"{base_url}/boom/1/")
    page.goto(f"{base_url}/boom/2/")

    wait_until(page, _list_url(base_url, query), lambda p: _row_count(p, base_url, query) >= 1)
    assert _row_count(page, base_url, query) == 1
    assert _issue_count(page, base_url, query) == 2


def test_storm_is_fast_and_stores_few_events(page, base_url, server_available) -> None:
    """[qa:manual-qa:storm-is-fast-and-stores-few-events] /storm/?n=5000 is fast and stores at
    most EVENT_SAMPLE_PER_HOUR occurrences."""
    login(page, base_url, "admin", "admin")
    query = "DemoStormError"
    _delete_existing_issue(page, base_url, query)

    start = time.monotonic()
    response = page.request.get(f"{base_url}/storm/?n=5000")
    wall_clock_seconds = time.monotonic() - start
    assert response.ok, f"storm request failed: {response.status}"
    body = response.json()
    assert body["elapsed_seconds"] < 1.0, f"view reported {body['elapsed_seconds']}s"
    assert wall_clock_seconds < 1.0, f"client measured {wall_clock_seconds}s"

    wait_until(page, _list_url(base_url, query), lambda p: _row_count(p, base_url, query) >= 1)
    # A substantial fraction of 5000, not all of it: the writer's queue (QUEUE_MAXSIZE=1000,
    # default) drops the newest item on overflow when a burst outruns the consumer, so not every
    # occurrence survives — verified locally at a stable ~1200/5000 across repeated runs. 500 is a
    # floor well below that observed value (room for a slower CI machine) and far above zero, so a
    # regression that silently drops the whole storm (not just the excess) still fails loudly.
    assert _issue_count(page, base_url, query) >= 500
    page.goto(_detail_url(page, base_url, query))
    stored_rows = page.locator("table.ae-events-table tbody tr td a")
    assert 1 <= stored_rows.count() <= EVENT_SAMPLE_PER_HOUR


def test_resolve_then_rehit_shows_regressed_badge(page, base_url, server_available) -> None:
    """[qa:manual-qa:resolve-then-rehit-shows-regressed-badge] Resolve, re-hit /boom/ ->
    Regressed badge.

    Reuses the /boom/ issue `test_three_boom_hits_make_one_issue_with_count_three` just
    (re)created above (same file, runs first), instead of deleting and re-hitting again: /boom/'s
    ZeroDivisionError fingerprint is also used by test_admin_ui.py, test_demo_surface.py and
    test_notifications_i18n.py, and a second delete+recreate here needlessly burns a second slice
    of its process-wide EVENT_SAMPLE_PER_HOUR budget for no assertion this test needs — that is
    what left the recreated issue with no stored `last_event`, breaking
    test_notifications_i18n.py's Copy-as-text case downstream (p10/T11, DECISIONS.md).

    Does not assert a regression email: the demo ships `NOTIFY_THROTTLE_SECONDS=3600` and
    resolving an issue does not reset `notified_at` by design (p09-review_fix2/minor-rejected2),
    so reusing this same-run issue means its throttle window is still open from the "New issue:"
    notification `test_three_boom_hits_make_one_issue_with_count_three` already fired — the
    regression email is suppressed by design, not a bug. The email path is covered at unit level
    by `tests/test_notifications.py::test_regression_sends_one_email`.
    """
    login(page, base_url, "admin", "admin")
    query = "ZeroDivisionError"
    detail_url = _detail_url(page, base_url, query)
    page.goto(detail_url)
    page.get_by_role("button", name="Resolve").click()
    page.wait_for_url(detail_url)
    assert page.locator(".ae-badge--resolved").count() == 1

    page.goto(f"{base_url}/boom/")
    wait_until(page, detail_url, lambda p: p.locator(".ae-badge--regressed").count() == 1)


def test_errors_cleanup_dry_run_prints_a_report(page, base_url, server_available) -> None:
    """[qa:manual-qa:errors-cleanup-dry-run-prints-a-report] `errors_cleanup --dry-run` exits 0,
    reports every retention rule, deletes nothing."""
    # `uv run --extra e2e pytest` runs under pytest-django, which sets DJANGO_SETTINGS_MODULE=
    # tests.settings in this process's own environment; demo/manage.py's `setdefault` then leaves
    # that inherited value in place instead of its own `demo_project.settings`. Override it
    # explicitly so the subprocess boots the demo project, not the test host.
    demo_env = {**os.environ, "DJANGO_SETTINGS_MODULE": "demo_project.settings"}
    before = subprocess.run(
        [sys.executable, "demo/manage.py", "errors_stats"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=demo_env,
    )
    assert before.returncode == 0, before.stderr

    dry_run = subprocess.run(
        [sys.executable, "demo/manage.py", "errors_cleanup", "--dry-run"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=demo_env,
    )
    assert dry_run.returncode == 0, dry_run.stderr
    assert "(dry run — nothing deleted)" in dry_run.stdout
    for label in RETENTION_LABELS:
        assert f"{label}:" in dry_run.stdout, f"missing {label!r} row in:\n{dry_run.stdout}"

    after = subprocess.run(
        [sys.executable, "demo/manage.py", "errors_stats"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=demo_env,
    )
    assert after.returncode == 0, after.stderr

    # Not a full stdout diff: the demo's writer thread can flush an unrelated, already-in-flight
    # occurrence between the two `errors_stats` calls, which legitimately advances "newest issue
    # last_seen" without the dry run itself deleting or creating anything. Row counts are what the
    # dry run promises to leave untouched, so only those lines need to match.
    def _count_lines(stdout: str) -> list[str]:
        stable_prefixes = ("issues", "events", "daily_counts")
        return [
            line
            for line in stdout.splitlines()
            if line.split(":", 1)[0] in stable_prefixes or line.startswith("status[")
        ]

    count_lines = _count_lines(before.stdout)
    assert count_lines, f"no row-count lines found in:\n{before.stdout}"
    assert count_lines == _count_lines(after.stdout), "dry run must not change any row counts"


def test_dark_mode_is_readable(browser, base_url, server_available) -> None:
    """[qa:manual-qa:dark-mode-is-readable] Issue list and detail pages render in dark mode with
    non-transparent, distinct badge/chart colours."""
    context = browser.new_context(color_scheme="dark")
    try:
        dark_page = context.new_page()
        login(dark_page, base_url, "admin", "admin")

        dark_page.goto(_list_url(base_url))
        badges = dark_page.locator("#result_list .ae-badge")
        assert badges.count() >= 1
        badge_color = badges.first.evaluate("el => getComputedStyle(el).color")
        body_bg = dark_page.evaluate("() => getComputedStyle(document.body).backgroundColor")
        assert badge_color not in ("", "rgba(0, 0, 0, 0)", "transparent")
        assert badge_color != body_bg

        rows = dark_page.locator("#result_list tbody tr")
        assert rows.count() > 0, "demo_seed should have produced issues"
        href = rows.first.locator("a.ae-issue-cell").get_attribute("href")
        dark_page.goto(urljoin(f"{base_url}/", href))

        chart = dark_page.locator(".ae-bar-chart, .ae-sparkline")
        assert chart.count() >= 1
        chart_color = chart.first.evaluate("el => getComputedStyle(el).color")
        assert chart_color not in ("", "rgba(0, 0, 0, 0)", "transparent")
    finally:
        context.close()
