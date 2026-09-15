"""T14 (PLAN.md phase 8): README screenshots of the admin's Errors section, light and dark.

Not a `[qa:...]`-tagged correctness case — this test's job is to produce
`docs/img/issue-{list,detail}-{light,dark}.png` from the real seeded demo, driving `/nested/` (a
chained exception, so the detail screenshot shows both blocks and the separator) and `/sensitive/`
(so the list has a second, distinct issue) before capturing. The admin's default theme is "auto";
a Playwright browser context's `color_scheme` flips it without touching any UI control.
"""

from pathlib import Path
from urllib.parse import urljoin

import pytest

pytest.importorskip("playwright.sync_api")

from e2e.conftest import login, wait_until

DOCS_IMG_DIR = Path(__file__).resolve().parent.parent / "docs" / "img"


def _capture_theme(browser, base_url: str, color_scheme: str) -> None:
    # The admin's own `.results` wrapper is `overflow-x: auto`: at the default 1280px viewport the
    # issue table (with the Status/Trend columns) is wider than that wrapper, so a `full_page`
    # screenshot (which only extends the *vertical* capture) silently clips them. 1600px is wide
    # enough for the table to render without internal scrolling (measured against the seeded demo).
    viewport = {"width": 1600, "height": 1000}
    context = browser.new_context(color_scheme=color_scheme, viewport=viewport)
    page = context.new_page()
    try:
        login(page, base_url, "admin", "admin")

        page.goto(f"{base_url}/nested/")
        page.goto(f"{base_url}/sensitive/")
        page.fill('input[name="username"]', "demo-user")
        page.fill('input[name="password"]', "demo-password")
        page.fill('input[name="token"]', "demo-token")
        page.click("button[type=submit]")

        list_url = f"{base_url}/admin/admin_errors/issue/"
        wait_until(
            page,
            f"{list_url}?q=nested",
            lambda p: p.locator("#result_list tbody tr").count() >= 1,
        )

        page.goto(list_url)
        page.screenshot(path=str(DOCS_IMG_DIR / f"issue-list-{color_scheme}.png"), full_page=True)

        page.goto(f"{list_url}?q=nested")
        href = page.locator("a.ae-issue-cell").first.get_attribute("href")
        page.goto(urljoin(f"{base_url}/", href))
        page.screenshot(path=str(DOCS_IMG_DIR / f"issue-detail-{color_scheme}.png"), full_page=True)
    finally:
        context.close()


@pytest.mark.screenshots
def test_generates_light_and_dark_screenshots(browser, base_url, server_available) -> None:
    """Produces the four README screenshots; look at them and record the visual check."""
    DOCS_IMG_DIR.mkdir(parents=True, exist_ok=True)

    for color_scheme in ("light", "dark"):
        _capture_theme(browser, base_url, color_scheme)

    for name in (
        "issue-list-light.png",
        "issue-list-dark.png",
        "issue-detail-light.png",
        "issue-detail-dark.png",
    ):
        path = DOCS_IMG_DIR / name
        assert path.exists() and path.stat().st_size > 0, f"{name} was not written"
