"""Views used only by the capture-pipeline tests (`tests/test_capture.py`,
`tests/test_context.py`)."""

import logging

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

logger = logging.getLogger("tests.views")


class UnrepresentableUser:
    """A `request.user`-shaped object that raises whenever it is inspected."""

    is_authenticated = True

    def __getattr__(self, name):
        raise RuntimeError("broken user")


def boom(request):
    raise ValueError("boom")


def nested(request):
    try:
        raise ValueError("inner failure")
    except ValueError as exc:
        raise RuntimeError("outer failure") from exc


def notfound(request):
    raise Http404("not found")


def denied(request):
    raise PermissionDenied("denied")


def log_and_raise(request):
    try:
        raise ValueError("log and raise")
    except ValueError:
        logger.exception("log and raise")
        raise


def log_only(request):
    try:
        raise ValueError("log only")
    except ValueError:
        logger.exception("log only")
    return HttpResponse("ok")


@sensitive_post_parameters("password")
def sensitive_post(request):
    # Deliberately does not read `request.POST["password"]` into a local: Django's
    # sensitive_post_parameters cleanses `request.POST` itself in the traceback locals and in the
    # request block, not a plain string later derived from it, so doing so would leak the value
    # through `frames[*].vars` regardless of the decorator (a `@sensitive_variables` problem, not
    # a `@sensitive_post_parameters` one, and out of scope for what this view exercises).
    raise ValueError("sensitive post")


@sensitive_variables("password")
def sensitive_vars(request):
    password = "super-secret"  # noqa: F841 - captured via the traceback locals, not read here
    raise ValueError("sensitive vars")


def unrepresentable(request):
    class _Unrepresentable:
        def __repr__(self):
            raise RuntimeError("cannot repr")

    obj = _Unrepresentable()  # noqa: F841 - captured via the traceback locals, not read here
    raise ValueError("unrepresentable")


def broken_user(request):
    request.user = UnrepresentableUser()
    raise ValueError("broken user")


async def aboom(request):
    raise ValueError("async boom")
