import urllib.request
from urllib.parse import urlsplit

from e2e.conftest import E2E_BASE_URL, ready_url


def test_harness_contract() -> None:
    parsed = urlsplit(E2E_BASE_URL)
    assert parsed.scheme in ("http", "https")
    assert ready_url() == f"{E2E_BASE_URL}/admin/login/"

    import admin_errors

    assert admin_errors.__version__


def test_login_page_reachable(server_available) -> None:
    with urllib.request.urlopen(ready_url(), timeout=5) as response:
        assert response.status == 200
        body = response.read().decode()
    assert 'type="password"' in body
