from importlib import import_module

from django.apps import AppConfig


class DistributorsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.distributors"

    def ready(self):
        # Imported for their side effects only: signals registers receivers,
        # and checks (Task 68j) registers a Django system check. Imported by
        # name rather than `from . import ...`, which would need an unused-
        # import suppression -- the floor this project holds itself to.
        import_module("apps.distributors.signals")
        import_module("apps.distributors.checks")
