"""T14 (PLAN.md phase 8): README screenshots of the admin's Errors section, light and dark.

Not a `[qa:...]`-tagged correctness case — this test's job is to produce
`docs/img/issue-{list,detail}-{light,dark}.png` from the real seeded demo.

Three things the first version of this file got wrong, and what it does instead:

* **One state, two themes.** The data-changing part (hitting `/nested/` and `/sensitive/`, then
  backfilling the history) now runs once, before either theme is captured, and each theme pass
  only navigates and screenshots. The light and dark pairs are the same page in two skins, which
  is the entire point of showing both, rather than two separate moments with different counts.

* **A detail page worth showing.** `/nested/` gives a chained exception (both blocks and the
  "direct cause" separator) and a real request block with scrubbed cookies, but an issue that is
  minutes old can only draw a single bar in the occurrence chart. `demo_seed --backfill-history`
  gives that same issue a 30-day history without touching its events or payload, so one
  screenshot shows the traceback, the scrubbed request *and* a chart with something in it.

* **A list that fits a README.** `full_page=True` captured all 44 rows — 3500px tall, which a
  README renders as an unreadable sliver. The list is now captured at the viewport, which holds
  the summary cards, the filters and a dozen rows.
"""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin

import pytest

pytest.importorskip("playwright.sync_api")

from e2e.conftest import login, wait_until

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_IMG_DIR = REPO_ROOT / "docs" / "img"

# The admin's own `.results` wrapper is `overflow-x: auto`: at the default 1280px viewport the
# issue table (with the Status/Trend columns) is wider than that wrapper, so a screenshot silently
# clips them. 1600px is wide enough for the table to render without internal scrolling (measured
# against the seeded demo). 1000px of height holds the summary cards, the search row and about a
# dozen issues — enough to show what the page is, short enough to read in a README.
VIEWPORT = {"width": 1600, "height": 1000}
HISTORY_DAYS = 30


def _prepare_data(browser, base_url: str) -> str:
    """Create the issues both themes are captured from. Returns the detail page's path."""
    context = browser.new_context(viewport=VIEWPORT)
    page = context.new_page()
    try:
        login(page, base_url, "admin", "admin")

        # /nested/ raises RuntimeError from an inner ValueError: the detail screenshot gets both
        # exception blocks and the "The above exception was the direct cause" separator.
        page.goto(f"{base_url}/nested/")
        # /sensitive/ posts a password, a token and an Authorization header, so the list has a
        # second recent issue and the demo has a scrubbing case on screen.
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

        page.goto(f"{list_url}?q=nested")
        detail_path = page.locator("a.ae-issue-cell").first.get_attribute("href")
    finally:
        context.close()

    issue_id = int(detail_path.rstrip("/").split("/")[-2])
    # pytest-django exports DJANGO_SETTINGS_MODULE=tests.settings, and `demo/manage.py` only
    # `setdefault`s its own, so an inherited environment would point this command at the test
    # settings (and their database) instead of the demo's.
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "demo_project.settings"}
    subprocess.run(
        [
            sys.executable,
            "demo/manage.py",
            "demo_seed",
            "--backfill-history",
            str(issue_id),
            "--days",
            str(HISTORY_DAYS),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return detail_path


def _capture_theme(browser, base_url: str, detail_path: str, color_scheme: str) -> None:
    context = browser.new_context(color_scheme=color_scheme, viewport=VIEWPORT)
    page = context.new_page()
    try:
        login(page, base_url, "admin", "admin")

        page.goto(f"{base_url}/admin/admin_errors/issue/")
        page.screenshot(path=str(DOCS_IMG_DIR / f"issue-list-{color_scheme}.png"))

        page.goto(urljoin(f"{base_url}/", detail_path))
        page.screenshot(path=str(DOCS_IMG_DIR / f"issue-detail-{color_scheme}.png"), full_page=True)
    finally:
        context.close()


@pytest.mark.screenshots
def test_generates_light_and_dark_screenshots(browser, base_url, server_available) -> None:
    """Produces the four README screenshots; look at them and record the visual check."""
    DOCS_IMG_DIR.mkdir(parents=True, exist_ok=True)

    detail_path = _prepare_data(browser, base_url)
    for color_scheme in ("light", "dark"):
        _capture_theme(browser, base_url, detail_path, color_scheme)

    for name in (
        "issue-list-light.png",
        "issue-list-dark.png",
        "issue-detail-light.png",
        "issue-detail-dark.png",
    ):
        path = DOCS_IMG_DIR / name
        assert path.exists() and path.stat().st_size > 0, f"{name} was not written"
