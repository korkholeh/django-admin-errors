try:
    from demo_project.celery import app as celery_app  # noqa: F401
except ImportError:
    pass
