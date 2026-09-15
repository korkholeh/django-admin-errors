"""`context.py`: the only module that touches untrusted host data.

Scrubbing is asserted by *absence* of the sensitive string from the built payload/frames, per
CLAUDE.md: a permission on a view is not a permission on a template include, and the same discipline
applies to the underlying data.
"""

import json
import sys

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import DisallowedHost
from django.test import Client, override_settings

from admin_errors import context
from admin_errors.models import Event
from tests import views

pytestmark = pytest.mark.django_db


def _raise_and_capture(view, request):
    try:
        view(request)
    except Exception:
        return sys.exc_info()
    raise AssertionError("view did not raise")


def test_undecorated_post_password_field_is_scrubbed_by_key(rf):
    """A view with no `@sensitive_post_parameters` still must not store a "password" POST value
    in clear text (spec section 2/14): `build_request_block` cleanses POST by key, the same way
    it already does for headers and cookies via `filter_.cleanse_setting`."""
    request = rf.post("/log-only/", {"password": "hunter2", "note": "visible"})
    block = context.build_request_block(request)
    assert "hunter2" not in json.dumps(block)
    assert block["post"]["note"] == "visible"


def test_repeated_post_and_get_keys_keep_all_values(rf):
    """`QueryDict.dict()` keeps only the last value of a repeated key; the payload must keep all
    of them so it does not misrepresent the request that caused the error."""
    request = rf.post("/log-only/?tags=x&tags=y", {"tags": ["a", "b", "c"]})
    block = context.build_request_block(request)
    assert block["post"]["tags"] == ["a", "b", "c"]
    assert block["query"]["tags"] == ["x", "y"]


def test_sensitive_post_parameters_decorator_scrubs_the_real_view():
    """Drives the decorated view end to end so the `@sensitive_post_parameters` wiring itself
    (not just `get_post_parameters`'s branch in isolation) is exercised."""
    client = Client(raise_request_exception=False)
    response = client.post("/sensitive-post/", {"password": "hunter2", "note": "visible"})

    assert response.status_code == 500
    event = Event.objects.get()
    assert "hunter2" not in json.dumps(event.payload["request"]["post"])
    assert event.payload["request"]["post"]["note"] == "visible"


def test_sensitive_variables_locals_are_absent_from_the_payload(rf):
    request = rf.get("/sensitive-vars/")
    exc_type, exc_value, tb = _raise_and_capture(views.sensitive_vars, request)
    frames = context.build_frames(request, exc_type, exc_value, tb)
    # The innermost frame is `sensitive_vars` itself; check the scrubbed *value*, not the source
    # lines around it (those legitimately show the raw `password = "super-secret"` source text).
    assert frames[-1]["vars"]["password"] != "'super-secret'"
    assert "super-secret" not in frames[-1]["vars"]["password"]


def test_sensitive_headers_are_absent_from_the_payload(rf):
    request = rf.get(
        "/boom/",
        HTTP_AUTHORIZATION="Bearer super-secret-token",
        HTTP_X_API_KEY="also-super-secret",
    )
    block = context.build_request_block(request)
    dumped = json.dumps(block["headers"])
    assert "super-secret-token" not in dumped
    assert "also-super-secret" not in dumped
    assert "Authorization" in block["headers"]
    assert "X-Api-Key" in block["headers"]


def test_sessionid_cookie_value_is_absent_from_the_payload(rf):
    request = rf.get("/boom/", HTTP_COOKIE="sessionid=super-secret-session; other=1")
    block = context.build_request_block(request)
    assert block["cookies"]["sessionid"] != "super-secret-session"
    assert "super-secret-session" not in json.dumps(block["cookies"])
    # The raw `Cookie` request header must be scrubbed too, not just the parsed `cookies` block.
    assert "super-secret-session" not in json.dumps(block["headers"])


def test_unrepresentable_object_becomes_the_fallback_string():
    class _Unrepresentable:
        def __repr__(self):
            raise RuntimeError("cannot repr")

    result = context.safe_repr(_Unrepresentable(), limit=200)
    assert result == "<unrepresentable _Unrepresentable>"


def test_disallowed_host_falls_back_to_path(rf):
    request = rf.get("/boom/", HTTP_HOST="evil.example.com")
    with override_settings(ALLOWED_HOSTS=["good.example.com"]):
        with pytest.raises(DisallowedHost):
            request.get_host()  # sanity: this host really is disallowed
        block = context.build_request_block(request)
    assert block["url"] == block["path"] == "/boom/"


def test_anonymous_user_produces_no_user_block(rf):
    request = rf.get("/boom/")
    request.user = AnonymousUser()
    block = context.build_request_block(request)
    assert block["user"] is None


def test_authenticated_user_is_included(rf):
    user = get_user_model().objects.create_user(username="oleh", email="oleh@example.com")
    request = rf.get("/boom/")
    request.user = user
    block = context.build_request_block(request)
    assert block["user"] == {"id": user.pk, "username": "oleh", "email": "oleh@example.com"}


def test_broken_request_user_does_not_break_the_payload(rf):
    request = rf.get("/broken-user/")
    request.user = views.UnrepresentableUser()
    block = context.build_request_block(request)
    assert block["user"] is None
    assert block["path"] == "/broken-user/"


def test_in_app_include_and_exclude():
    overrides = {"IN_APP_INCLUDE": ["/app/shop"], "IN_APP_EXCLUDE": ["site-packages"]}
    with override_settings(ADMIN_ERRORS=overrides):
        assert context.is_in_app("/app/shop/views.py") is True
        assert context.is_in_app("/usr/lib/site-packages/django/core.py") is False
        assert context.is_in_app("/other/place/mod.py") is False


def test_in_app_falls_back_to_true_when_no_prefix_is_resolvable():
    # tests/settings.py defines no BASE_DIR, so the default IN_APP_INCLUDE=None resolves to no
    # prefixes at all: every non-excluded frame counts as in-app.
    assert context.is_in_app("/anywhere/mod.py") is True


def test_in_app_exclude_wins_even_without_include():
    assert context.is_in_app("/x/site-packages/pkg/mod.py") is False


def test_chained_exceptions_produce_chain_entries(rf):
    request = rf.get("/nested/")
    exc_type, exc_value, tb = _raise_and_capture(views.nested, request)
    block = context.build_exception_block(request, exc_type, exc_value, tb)
    assert [node["type"] for node in block["chain"]] == ["RuntimeError", "ValueError"]
    assert block["chain"][0]["cause"] is True
    assert block["type"] == "RuntimeError"


def test_sanitize_text_replaces_nul_and_lone_surrogates_leaves_ordinary_text_alone():
    assert context.sanitize_text("boom\x00message") == "boom�message"
    assert context.sanitize_text("lone \ud800 surrogate") == "lone � surrogate"
    assert context.sanitize_text("plain text 💥 з non-ASCII") == "plain text 💥 з non-ASCII"


def test_sanitize_payload_recurses_through_nested_dicts_lists_and_keys():
    """`sanitize_payload` is applied once, late, by `capture._build_and_store` — after the `extra`
    merge and `BEFORE_SEND` (DECISIONS.md p06-review_fix1/context) — so this exercises the walk
    itself: values *and* keys, at any nesting depth. A NUL in a dict *key* (e.g. a query-param
    name) is the case that regressed silently: only values used to be sanitized."""
    payload = {
        "message": "top-level\x00message",
        "request": {"query": {"bad\x00key": "boom\x00end", "ok": ["a\x00b", "c"]}},
    }

    sanitized = context.sanitize_payload(payload)

    assert sanitized["message"] == "top-level�message"
    assert sanitized["request"]["query"]["bad�key"] == "boom�end"
    assert sanitized["request"]["query"]["ok"] == ["a�b", "c"]
