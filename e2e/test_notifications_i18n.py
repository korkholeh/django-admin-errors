"""Plan: e2e/plans/notifications-i18n.plan.yaml. Real browser + the demo's own stdout: a
created-issue email actually leaves the process (console `EMAIL_BACKEND`), a resolve/re-hit cycle
flips the status badge to Regressed, and "Copy as text" really uses the Clipboard API.
"""

import time
from pathlib import Path
from urllib.parse import urljoin

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import expect

from e2e.conftest import login, wait_until

ISSUE_LIST_PATH = "/admin/admin_errors/issue/"
SERVER_LOG_PATH = Path("/tmp/admin-errors-e2e-server.log")


def _list_url(base_url: str, query: str | None = None) -> str:
    url = f"{base_url}{ISSUE_LIST_PATH}"
    return f"{url}?q={query}" if query else url


def _row_count(page, base_url: str, query: str) -> int:
    page.goto(_list_url(base_url, query))
    return page.locator("#result_list tbody tr").count()


def _detail_url(page, base_url: str, query: str) -> str:
    page.goto(_list_url(base_url, query))
    rows = page.locator("#result_list tbody tr")
    assert rows.count() == 1, f"expected exactly one row for {query!r}, got {rows.count()}"
    href = rows.first.locator("a.ae-issue-cell").get_attribute("href")
    return urljoin(f"{base_url}/", href)


def _server_log_offset() -> int:
    return SERVER_LOG_PATH.stat().st_size if SERVER_LOG_PATH.exists() else 0


def _server_log_contains(*needles: str, since: int = 0) -> bool:
    if not SERVER_LOG_PATH.exists():
        return False
    with SERVER_LOG_PATH.open("rb") as handle:
        handle.seek(since)
        text = handle.read().decode(errors="replace")
    return all(needle in text for needle in needles)


def _delete_existing_issue(page, base_url: str, query: str) -> None:
    """Delete any pre-existing issue for `query`, through the admin's own bulk-delete action.

    `make e2e-up` is idempotent and does not restart an already-running demo server, so a retry of
    this test (or an earlier manual QA pass — see `.autodev/DECISIONS.md` p09-e2e) can find
    `demo_app.views.logged`'s issue already created and already notified, inside the default
    3600s `NOTIFY_THROTTLE_SECONDS` window. That leaves this run's own hit silently un-notified —
    not a product bug, a stale precondition. Deleting the row first (a real admin action, not a
    DB reach-around) re-establishes "never hit before in this server run" unconditionally.
    """
    page.goto(_list_url(base_url, query))
    rows = page.locator("#result_list tbody tr")
    if rows.count() == 0:
        return
    page.locator("#action-toggle").check()
    page.locator("select[name=action]").select_option("delete_selected")
    page.locator("button[title='Run the selected action']").click()
    # The delete-confirmation page's only submit control is the "Yes, I'm sure" button (a "No,
    # take me back" link is not a submit); matched by input type to dodge the localized curly
    # apostrophe in its label rather than a text locator.
    page.locator("input[type=submit]").click()
    assert _row_count(page, base_url, query) == 0, f"failed to clear stale issue for {query!r}"


def _hit_until_admitted(
    page, base_url: str, path: str, query: str, *, timeout_seconds: float = 30.0
) -> None:
    """`GET path` repeatedly until an issue shows up in the list search for `query`.

    A single occurrence of a brand-new fingerprint can be silently dropped by the
    `NEW_ISSUES_PER_MINUTE` admission bucket (spec section 7.2 step 4) when another e2e case
    (`demo-app-surface`'s `/unique-storm/` burst) has just drained it — that is not a writer-flush
    delay `wait_until` alone can wait out, since the request that would have created the row never
    admitted it. Re-firing the request is what a real, persistent user would do, and the bucket
    refills fast enough (~1.2s per token from empty) that this converges well inside the timeout.
    Once one occurrence is admitted the fingerprint is permanently in the writer's seen-set, so
    every later hit to the same culprit (any `/keyerror/<key>/`) always succeeds, free of charge.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        page.goto(f"{base_url}{path}")
        if _row_count(page, base_url, query) >= 1:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(f"issue for {query!r} never admitted within {timeout_seconds}s")
        time.sleep(0.5)


def test_created_email_in_server_log_and_regressed_badge(page, base_url, server_available) -> None:
    """[qa:notifications-i18n:created-email-in-server-log-and-regressed-badge] Console email +
    regressed badge."""
    login(page, base_url, "admin", "admin")
    # `/logged/` takes no parameter, so every hit maps to the same issue (culprit
    # `demo_app.views.logged`) by construction — unlike `/keyerror/<key>/`, no other e2e spec
    # (`test_admin_ui.py`, `test_screenshots.py`) touches this culprit, so this test owns the row
    # outright (review r2 blocker: `/keyerror/` was shared with `admin-ui.plan.yaml`'s own
    # regression case, so whichever e2e file ran first consumed the only "New issue:" line).
    # `_delete_existing_issue` below then clears any leftover row from a previous attempt against
    # the same long-lived demo server, so the "New issue:" line this run produces is always fresh.
    culprit_query = "demo_app.views.logged"

    _delete_existing_issue(page, base_url, culprit_query)

    log_offset = _server_log_offset()
    _hit_until_admitted(page, base_url, "/logged/", culprit_query)

    deadline = time.monotonic() + 10.0
    while not _server_log_contains("New issue:", culprit_query, since=log_offset):
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"expected a 'New issue:' notification for {culprit_query!r} in {SERVER_LOG_PATH}"
            )
        time.sleep(0.5)

    detail_url = _detail_url(page, base_url, culprit_query)
    page.goto(detail_url)
    page.get_by_role("button", name="Resolve").click()
    page.wait_for_url(detail_url)
    assert page.locator(".ae-badge--resolved").count() == 1

    page.goto(f"{base_url}/logged/")
    wait_until(page, detail_url, lambda p: p.locator(".ae-badge--regressed").count() == 1)


def test_copy_as_text_flips_to_copied(page, base_url, server_available) -> None:
    """[qa:notifications-i18n:copy-as-text-flips-to-copied] Copy as text flips its label."""
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base_url)
    login(page, base_url, "admin", "admin")

    page.goto(f"{base_url}/boom/")
    wait_until(
        page,
        _list_url(base_url, "ZeroDivisionError"),
        lambda p: _row_count(p, base_url, "ZeroDivisionError") >= 1,
    )
    detail_url = _detail_url(page, base_url, "ZeroDivisionError")
    page.goto(detail_url)

    pre = page.locator("#ae-traceback-text")
    assert pre.count() == 1
    traceback_text = pre.text_content().strip()
    assert traceback_text, "expected the hidden pre to carry the rendered plain-text traceback"

    button = page.locator("button.ae-copy")
    expect(button).to_have_text("Copy as text")
    button.click()
    # Assert on the non-self-clearing counter, not the visible label: the label reverts after 2s
    # on the same timer, so a slow first poll on a loaded machine could otherwise race it and
    # produce a false negative unrelated to whether the copy actually happened (review r2 minor).
    expect(button).to_have_attribute("data-ae-copy-count", "1")
    expect(button).to_have_text("Copied")
