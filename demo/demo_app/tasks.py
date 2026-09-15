"""`/task/` dispatches `fail_task`. A `shared_task` when Celery is installed (eager, see
`demo_project/celery.py`), a plain function otherwise — `views.task` calls `.delay()` when it
exists and falls back to a direct call + `api.capture_exception()`, so both paths produce exactly
one issue.
"""

try:
    from celery import shared_task
except ImportError:
    shared_task = None


class DemoTaskError(Exception):
    pass


def _fail() -> None:
    raise DemoTaskError("background task failed")


if shared_task is not None:
    fail_task = shared_task(name="demo_app.fail_task")(_fail)
else:
    fail_task = _fail
