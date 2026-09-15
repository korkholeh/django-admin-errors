from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class AdminErrorsConfig(AppConfig):
    name = "admin_errors"
    label = "admin_errors"
    verbose_name = _("Errors")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        """Register system checks, install the logging handler, connect the marker receiver.

        Must never query the database, start a thread, or touch the filesystem here.
        """
        from django.core.checks import Tags, register
        from django.core.signals import got_request_exception

        from admin_errors import checks, signals
        from admin_errors.conf import settings as admin_errors_settings

        register(checks.check_settings_keys, Tags.compatibility)
        register(checks.check_sqlite_version, Tags.compatibility)

        if admin_errors_settings.AUTO_INSTALL_LOGGING_HANDLER:
            self._install_logging_handler()

        got_request_exception.connect(
            signals.mark_request_on_exception,
            dispatch_uid="admin_errors.mark_request_on_exception",
        )

    @staticmethod
    def _install_logging_handler() -> None:
        import logging

        from admin_errors.conf import settings as admin_errors_settings
        from admin_errors.handlers import AdminErrorsHandler

        root = logging.getLogger()
        if any(isinstance(handler, AdminErrorsHandler) for handler in root.handlers):
            return
        handler = AdminErrorsHandler()
        handler.setLevel(admin_errors_settings.CAPTURE_LEVEL)
        root.addHandler(handler)
        # The root logger defaults to WARNING; a handler alone never sees records below its
        # logger's level, so a lower `CAPTURE_LEVEL` (e.g. "INFO") would otherwise be silently
        # unreachable.
        if handler.level and handler.level < root.getEffectiveLevel():
            root.setLevel(handler.level)
