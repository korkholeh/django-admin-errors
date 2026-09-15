from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AdminErrorsConfig(AppConfig):
    name = "admin_errors"
    label = "admin_errors"
    verbose_name = _("Errors")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Register system checks. Phase 3 installs the logging handler.

        Must never query the database, start a thread, or touch the filesystem here.
        """
        from django.core.checks import Tags, register

        from admin_errors import checks

        register(checks.check_settings_keys, Tags.compatibility)
        register(checks.check_sqlite_version, Tags.compatibility)
