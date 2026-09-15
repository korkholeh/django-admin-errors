"""Plan: e2e/plans/admin-login.plan.yaml. Real browser against the demo admin login (spec section
13's manual QA script, step 1). Not a second copy of `tests/test_demo.py`'s URL sweep: the one
thing only a browser can prove is that the login form and session cookie actually work end to end.
"""

import pytest

pytest.importorskip("playwright.sync_api")


def test_admin_login_reaches_the_index(page, base_url, server_available) -> None:
    """[qa:admin-login:happy-path] admin/admin logs in and reaches the admin index."""
    page.goto(f"{base_url}/admin/login/")
    page.fill("#id_username", "admin")
    page.fill("#id_password", "admin")
    page.click("input[type=submit]")

    page.wait_for_url(f"{base_url}/admin/")
    assert "/admin/" in page.url
    assert page.locator("#site-name").count() > 0


def test_wrong_password_stays_on_login_page(page, base_url, server_available) -> None:
    """[qa:admin-login:wrong-password] A wrong password is rejected and never reaches the index."""
    page.goto(f"{base_url}/admin/login/")
    page.fill("#id_username", "admin")
    page.fill("#id_password", "not-the-password")
    page.click("input[type=submit]")

    page.wait_for_load_state("networkidle")
    assert page.url.startswith(f"{base_url}/admin/login/")
    assert page.get_by_text("Please enter the correct username and password").count() > 0


def test_session_persists_on_reload(page, base_url, server_available) -> None:
    """[qa:admin-login:session-persists-on-reload] A logged-in session survives a page reload."""
    page.goto(f"{base_url}/admin/login/")
    page.fill("#id_username", "admin")
    page.fill("#id_password", "admin")
    page.click("input[type=submit]")
    page.wait_for_url(f"{base_url}/admin/")

    page.reload()

    assert page.url.startswith(f"{base_url}/admin/")
    assert page.locator("#site-name").count() > 0
