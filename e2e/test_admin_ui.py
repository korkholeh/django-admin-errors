"""Plan: e2e/plans/admin-ui.plan.yaml. Real browser against the admin's Errors section — the
manual QA script from docs/spec.md §13, driven by Playwright instead of by hand: triage from the
issue list, read a traceback, resolve/re-hit to see a regressed badge, and prove the permission
gate holds for a real cookie-authenticated session, not just a `Client()` call.
"""

from urllib.parse import urljoin

import pytest

pytest.importorskip("playwright.sync_api")

from e2e.conftest import login, wait_until

ISSUE_LIST_PATH = "/admin/admin_errors/issue/"


def _list_url(base_url: str, query: str | None = None) -> str:
    url = f"{base_url}{ISSUE_LIST_PATH}"
    return f"{url}?q={query}" if query else url


def _row_count(page, base_url: str, query: str) -> int:
    """Number of matching rows in the issue list search for `query` (0 if none)."""
    page.goto(_list_url(base_url, query))
    return page.locator("#result_list tbody tr").count()


def _issue_count(page, base_url: str, query: str) -> int:
    """The `Count` column of the single row matching `query` search (0 if the row is absent)."""
    page.goto(_list_url(base_url, query))
    rows = page.locator("#result_list tbody tr")
    if rows.count() == 0:
        return 0
    assert rows.count() == 1, f"expected exactly one row for {query!r}, got {rows.count()}"
    return int(rows.first.locator("td.field-count_display").inner_text().strip())


def _detail_url(page, base_url: str, query: str) -> str:
    """Navigate the search for `query` and return the absolute detail URL of its single row."""
    page.goto(_list_url(base_url, query))
    rows = page.locator("#result_list tbody tr")
    assert rows.count() == 1, f"expected exactly one row for {query!r}, got {rows.count()}"
    href = rows.first.locator("a.ae-issue-cell").get_attribute("href")
    return urljoin(f"{base_url}/", href)


def test_issue_list_cards_and_sparkline(page, base_url, server_available) -> None:
    """[qa:admin-ui:issue-list-cards-and-sparkline] Summary cards and a sparkline render."""
    login(page, base_url, "admin", "admin")
    page.goto(_list_url(base_url))

    cards = page.locator(".ae-card")
    assert cards.count() == 3
    for i in range(3):
        assert cards.nth(i).locator(".ae-card-value").inner_text().strip().isdigit()

    rows = page.locator("#result_list tbody tr")
    assert rows.count() > 0, "demo_seed should have produced issues"
    assert page.locator("#result_list tbody tr td.field-trend svg").count() > 0


def test_boom_three_hits_count_3(page, base_url, server_available) -> None:
    """[qa:admin-ui:boom-three-hits-count-3] Three /boom/ hits add 3 to one issue's count."""
    login(page, base_url, "admin", "admin")
    before = _issue_count(page, base_url, "ZeroDivisionError")

    for _ in range(3):
        page.goto(f"{base_url}/boom/")

    wait_until(
        page,
        _list_url(base_url, "ZeroDivisionError"),
        lambda p: (
            _row_count(p, base_url, "ZeroDivisionError") == 1
            and int(p.locator("td.field-count_display").first.inner_text().strip()) == before + 3
        ),
    )
    assert _row_count(page, base_url, "ZeroDivisionError") == 1


def test_boom_n_collapse(page, base_url, server_available) -> None:
    """[qa:admin-ui:boom-n-collapse] /boom/1/ and /boom/2/ collapse into one distinct issue."""
    login(page, base_url, "admin", "admin")
    before = _issue_count(page, base_url, "boom_n")

    page.goto(f"{base_url}/boom/1/")
    page.goto(f"{base_url}/boom/2/")

    wait_until(
        page,
        _list_url(base_url, "boom_n"),
        lambda p: (
            _row_count(p, base_url, "boom_n") == 1
            and int(p.locator("td.field-count_display").first.inner_text().strip()) == before + 2
        ),
    )

    page.goto(_list_url(base_url, "boom_n"))
    row = page.locator("#result_list tbody tr").first
    assert "ValueError" in row.locator(".ae-issue-type").inner_text()
    # Distinct from /boom/'s own ZeroDivisionError issue, per the Phase 7 README correction:
    # the two never share a row, so filtering by exception type never returns this one.
    assert "ZeroDivisionError" not in row.locator(".ae-issue-type").inner_text()


def test_detail_traceback_collapsed_library_frame(page, base_url, server_available) -> None:
    """[qa:admin-ui:detail-traceback-collapsed-library-frame] A library frame is collapsed."""
    login(page, base_url, "admin", "admin")
    page.goto(f"{base_url}/boom/999999/")
    wait_until(
        page, _list_url(base_url, "boom_n"), lambda p: _row_count(p, base_url, "boom_n") == 1
    )

    detail_url = _detail_url(page, base_url, "boom_n")
    page.goto(detail_url)

    library_frames = page.locator(".ae-frame--library")
    assert library_frames.count() > 0, "expected at least one non-in-app frame in the traceback"
    frame = library_frames.first
    context = frame.locator(".ae-frame-context")
    toggle = frame.locator(".ae-frame-toggle")
    assert not context.is_visible()
    assert toggle.get_attribute("aria-expanded") == "false"

    toggle.click()
    assert context.is_visible()
    assert toggle.get_attribute("aria-expanded") == "true"


def test_locals_toggle(page, base_url, server_available) -> None:
    """[qa:admin-ui:locals-toggle] The in-app frame's Locals details toggle reveals `n`."""
    login(page, base_url, "admin", "admin")
    page.goto(f"{base_url}/boom/1234567/")
    wait_until(
        page, _list_url(base_url, "boom_n"), lambda p: _row_count(p, base_url, "boom_n") == 1
    )

    detail_url = _detail_url(page, base_url, "boom_n")
    page.goto(detail_url)

    in_app_frame = page.locator(".ae-frame--in-app").first
    details = in_app_frame.locator(".ae-frame-locals")
    assert details.count() == 1, "expected the in-app boom_n frame to carry captured locals"
    assert not details.locator("table").is_visible()

    details.locator("summary").click()

    assert details.locator("table").is_visible()
    assert details.locator("table").get_by_text("n", exact=True).count() > 0


def test_nested_chain_separator(page, base_url, server_available) -> None:
    """[qa:admin-ui:nested-chain-separator] Root cause first, direct-cause separator shown."""
    login(page, base_url, "admin", "admin")
    page.goto(f"{base_url}/nested/")
    wait_until(
        page, _list_url(base_url, "nested"), lambda p: _row_count(p, base_url, "nested") >= 1
    )

    detail_url = _detail_url(page, base_url, "nested")
    page.goto(detail_url)

    blocks = page.locator(".ae-traceback-block")
    assert blocks.count() == 2
    assert "ValueError" in blocks.nth(0).locator(".ae-exc-heading").inner_text()
    assert "RuntimeError" in blocks.nth(1).locator(".ae-exc-heading").inner_text()

    separator = page.locator(".ae-traceback-separator")
    assert separator.count() == 1
    assert "direct cause" in separator.inner_text()


def test_resolve_then_rehit_regressed_badge(page, base_url, server_available) -> None:
    """[qa:admin-ui:resolve-then-rehit-regressed-badge] Resolve, re-hit, badge says Regressed."""
    login(page, base_url, "admin", "admin")
    page.goto(f"{base_url}/keyerror/e2e-regression-badge/")
    wait_until(
        page,
        _list_url(base_url, "e2e-regression-badge"),
        lambda p: _row_count(p, base_url, "e2e-regression-badge") >= 1,
    )

    detail_url = _detail_url(page, base_url, "e2e-regression-badge")
    page.goto(detail_url)
    page.get_by_role("button", name="Resolve").click()
    page.wait_for_url(detail_url)
    assert page.locator(".ae-badge--resolved").count() == 1

    page.goto(f"{base_url}/keyerror/e2e-regression-badge/")
    wait_until(page, detail_url, lambda p: p.locator(".ae-badge--regressed").count() == 1)


def test_viewer_has_no_context_and_403_on_status_post(page, base_url, server_available) -> None:
    """[qa:admin-ui:viewer-has-no-context-and-403-on-status-post] view_issue only, gated."""
    login(page, base_url, "viewer", "viewer")
    page.goto(_list_url(base_url))
    assert page.locator("#result_list tbody tr").count() > 0

    rows = page.locator("#result_list tbody tr")
    href = rows.first.locator("a.ae-issue-cell").get_attribute("href")
    detail_url = urljoin(f"{base_url}/", href)
    page.goto(detail_url)

    assert page.get_by_role("button", name="Resolve").count() == 0
    assert page.get_by_role("button", name="Ignore").count() == 0
    assert page.get_by_role("button", name="Reopen").count() == 0
    for heading in ("GET", "POST", "Headers", "Cookies"):
        assert page.get_by_role("heading", name=heading, exact=True).count() == 0

    status_url = detail_url.rstrip("/").rsplit("/change", 1)[0] + "/status/resolve/"
    csrf_cookie = next(c for c in page.context.cookies() if c["name"] == "csrftoken")
    response = page.request.post(status_url, headers={"X-CSRFToken": csrf_cookie["value"]})
    assert response.status == 403
