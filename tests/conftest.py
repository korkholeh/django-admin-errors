from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_admin_errors_capture_state():
    """Reset process-local capture state so tests don't leak into each other.

    The recursion guard is thread-local and the internal-logging dedup set, the admission/sampling
    buckets and the writer thread/stats are process-global; none of these are reset by Django's
    `override_settings`/test isolation.
    """
    from admin_errors import capture, writer

    capture._recursion_guard.active = False
    capture._internal_logged_types.clear()
    capture.reset_rate_limits()
    writer.reset_for_tests()
    yield
    capture._recursion_guard.active = False
    capture._internal_logged_types.clear()
    capture.reset_rate_limits()
    writer.reset_for_tests()
