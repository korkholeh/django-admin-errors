"""The demo's URL map (spec section 13, docs/spec.md#13-demo-project).

Every view here is a hand-operable probe for one capture behaviour: aggregation, message
normalization, chained exceptions, `logger.exception`/`logger.warning`, scrubbing, admission and
sampling limiters, Celery, async views, and the `Http404` negative case.
"""

import logging
import time

from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseNotFound, JsonResponse
from django.shortcuts import render
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_POST

from admin_errors import api, capture

logger = logging.getLogger("demo_app")

# Fixed constants so `tests/test_demo.py` can assert their *absence* from the stored payload.
DEMO_PASSWORD = "hunter2-demo-password"
DEMO_TOKEN = "demo-secret-token-abc123"

_STORM_MAX_N = 100_000


def _clamp_n(request, default: int) -> int:
    try:
        n = int(request.GET.get("n", default))
    except (TypeError, ValueError):
        n = default
    return max(0, min(n, _STORM_MAX_N))


def index(request):
    links = [
        ("/boom/", "Unhandled ZeroDivisionError (500)"),
        ("/boom/7/", "Unhandled ValueError with a number in the message (500)"),
        ("/keyerror/some-key/", "Unhandled KeyError (500)"),
        ("/nested/", "Chained exception: RuntimeError raised from a ValueError (500)"),
        ("/logged/", "Caught exception, logger.exception(...), returns 200"),
        ("/warning/", "logger.warning(...) with extra context, returns 200"),
        ("/sensitive/", "Form with password/token fields, demonstrates scrubbing"),
        ("/storm/?n=1000", "1000 occurrences of the same error, demonstrates aggregation"),
        ("/unique-storm/?n=200", "200 unique errors, demonstrates NEW_ISSUES_PER_MINUTE"),
        ("/task/", "Dispatches a failing Celery task (eager by default)"),
        ("/async-boom/", "Unhandled RuntimeError in an async view (500)"),
        ("/404/", "Http404 - must not be captured"),
    ]
    return render(request, "demo_app/index.html", {"links": links})


def boom(request):
    return HttpResponse(str(1 / 0))


def boom_n(request, n):
    raise ValueError(f"bad value {n}")


def keyerror(request, key):
    return HttpResponse({}[key])


def nested(request):
    try:
        raise ValueError("inner failure")
    except ValueError as exc:
        raise RuntimeError("outer failure") from exc


def logged(request):
    try:
        raise ValueError("handled and logged")
    except ValueError:
        logger.exception("something went wrong, but we handled it")
    return HttpResponse("ok, logged")


def warning(request):
    logger.warning(
        "Slow payment provider %s",
        "acme-pay",
        extra={"provider": "acme-pay", "latency_ms": 4200},
    )
    return HttpResponse("ok, warned")


@sensitive_post_parameters("password")
@sensitive_variables("token")
def sensitive(request):
    if request.method == "GET":
        return render(request, "demo_app/sensitive_form.html")
    # captured via traceback locals, scrubbed by @sensitive_variables
    token = request.POST.get("token") or DEMO_TOKEN
    request.META["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    raise ValueError(f"sensitive submission from user {request.POST.get('username', '?')}")


class DemoStormError(Exception):
    pass


def _raise_storm_error(i):
    raise DemoStormError(f"storm event {i}")


def storm(request):
    n = _clamp_n(request, 1000)
    start = time.monotonic()
    fp = None
    for i in range(n):
        try:
            _raise_storm_error(i)
        except DemoStormError:
            fp = api.capture_exception()
    elapsed = time.monotonic() - start
    return JsonResponse({"n": n, "elapsed_seconds": elapsed, "fingerprint": fp})


def unique_storm(request):
    n = _clamp_n(request, 200)
    for i in range(n):
        api.capture_message(f"unique storm message {i}", fingerprint=f"demo-unique-storm-{i}")
    return JsonResponse({"n": n})


def task(request):
    from demo_app.tasks import fail_task

    delay = getattr(fail_task, "delay", None)
    if delay is not None:
        delay()
    else:
        try:
            fail_task()
        except Exception:
            api.capture_exception()
    return HttpResponse("ok, task dispatched")


async def async_boom(request):
    raise RuntimeError("async boom")


def not_found(request):
    raise Http404("not found on purpose")


@require_POST
def reset_rate_limits(request):
    """Test-only hook: clear the in-process admission/sampling state (`capture.reset_rate_limits`).

    `NEW_ISSUES_PER_MINUTE`/`EVENT_SAMPLE_PER_HOUR` token buckets are process-global and persist
    for as long as this `runserver` stays up, so a shared fingerprint's budget (`/boom/`'s
    ZeroDivisionError, `/storm/`'s DemoStormError) does not refill between separate e2e runs
    against a reused server (`make e2e-up` is idempotent by design). `demo/` is never packaged
    (CLAUDE.md), so this is not part of the library's surface; `DEBUG` gates it so a misconfigured
    prod-like deployment of the demo can't have its rate limits wiped by an anonymous POST.
    """
    if not settings.DEBUG:
        return HttpResponseNotFound()
    capture.reset_rate_limits()
    return HttpResponse("ok, rate limits reset")
