from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AdminErrorsConfig(AppConfig):
    name = "admin_errors"
    label = "admin_errors"
    verbose_name = _("Errors")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Nothing yet: Phase 2 registers checks, Phase 3 installs the handler.

        Must never query the database, start a thread, or touch the filesystem here.
        """
