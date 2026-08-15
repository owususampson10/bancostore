from django.apps import AppConfig


class PlatformSettingsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.platform_settings"

    def ready(self):
        # Task 47d. Imported here, not at module top level -- Django's
        # own standard convention for wiring signal receivers, avoiding
        # AppRegistryNotReady before every app's models have loaded.
        from . import signals  # noqa: F401
