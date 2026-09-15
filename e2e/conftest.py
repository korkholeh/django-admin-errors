"""E2E harness fixtures. No top-level `playwright` import: this module must stay importable
even when the `e2e` extra is not installed.
"""

import os
import urllib.request
from urllib.error import URLError

import pytest

E2E_BASE_URL = os.environ.get("ADMIN_ERRORS_E2E_BASE_URL", "http://127.0.0.1:8000")


def ready_url() -> str:
    return f"{E2E_BASE_URL}/admin/login/"


@pytest.fixture(scope="session")
def server_available() -> None:
    try:
        urllib.request.urlopen(ready_url(), timeout=2)
    except (URLError, OSError):
        pytest.skip(f"no demo server on {ready_url()}; run `make e2e-up` first")
