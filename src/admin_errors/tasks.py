"""The Celery-beat entry point (spec section 9.3): `admin_errors.tasks.cleanup`.

Importing this module must never fail, with or without Celery installed, so a host (or a test) can
always do `import admin_errors.tasks` without a guard — only the task itself is conditional on
`shared_task` actually being importable.
"""

from __future__ import annotations

import dataclasses

from admin_errors import retention
from admin_errors.conf import settings as conf

try:
    from celery import shared_task
except ImportError:
    shared_task = None

if shared_task is not None:

    @shared_task(name="admin_errors.cleanup")
    def cleanup(vacuum: bool = False) -> dict[str, object]:
        return dataclasses.asdict(retention.run_cleanup(using=conf.DATABASE, vacuum=vacuum))
