"""Plan: e2e/plans/demo-app-surface.plan.yaml. Real browser against the demo app's own pages —
the human-visible half of the URL map that `tests/test_demo.py` only ever probes with a test
client. No login needed: every URL here is a plain view.
"""

import pytest

pytest.importorskip("playwright.sync_api")


def test_index_lists_every_probe(page, base_url, server_available) -> None:
    """[qa:demo-app-surface:index-lists-every-probe] The index links to every demo probe."""
    page.goto(f"{base_url}/")

    for href in ("/boom/", "/sensitive/", "/storm/?n=1000", "/404/"):
        assert page.locator(f'a[href="{href}"]').count() > 0, f"missing link to {href}"


def test_sensitive_form_has_password_and_token_fields(page, base_url, server_available) -> None:
    """[qa:demo-app-surface:sensitive-form-has-password-and-token-fields] Both fields render."""
    page.goto(f"{base_url}/sensitive/")

    assert page.locator('input[name="username"]').count() == 1
    assert page.locator('input[name="password"][type="password"]').count() == 1
    assert page.locator('input[name="token"]').count() == 1


def test_boom_renders_technical_500_page(page, base_url, server_available) -> None:
    """[qa:demo-app-surface:boom-renders-technical-500-page] Django's technical 500 page renders."""
    response = page.goto(f"{base_url}/boom/")

    assert response.status == 500
    assert "ZeroDivisionError" in page.content()


def test_not_found_renders_technical_404_page(page, base_url, server_available) -> None:
    """[qa:demo-app-surface:not-found-renders-technical-404-page] The Http404 case returns 404."""
    response = page.goto(f"{base_url}/404/")

    assert response.status == 404
