"""Only imported when the `celery` extra is installed (see `demo_project/__init__.py`).

`task_always_eager` makes `demo_app.tasks.fail_task.delay()` run inline, in-process, so the demo
needs no broker and no worker.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo_project.settings")

app = Celery("demo")
app.conf.task_always_eager = True
app.autodiscover_tasks(["demo_app"])
