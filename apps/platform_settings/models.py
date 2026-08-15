from django.conf import settings
from django.db import models


class PlatformSettingChange(models.Model):
    """Task 47d. django-constance stores every business-rule setting via
    its own key/value backend (CONSTANCE_BACKEND =
    constance.backends.database.DatabaseBackend, a `Constance` model with
    just `key`/`value` -- one shared table for all 76+ settings, not one
    row per setting the way `HistoricalRecords()` expects to shadow).
    There's no model instance for `HistoricalRecords()` to attach to
    here, so this is a bespoke append-only audit log instead, populated
    by `apps.platform_settings.signals` listening to constance's own
    `config_updated` signal -- see that module's docstring for why a
    signal, not a form/view hook, and how it resolves `changed_by`.

    `old_value`/`new_value` are stored as plain text (`str(value)`), not
    the original Python type constance itself uses (Decimal, bool, int,
    str, ...) -- this is a human-readable audit log, not a mechanism for
    reconstructing/replaying a past value programmatically."""

    key = models.CharField(max_length=255, db_index=True)
    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="platform_setting_changes",
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at"]

    def __str__(self):
        return f"PlatformSettingChange<{self.key} at {self.changed_at.isoformat()}>"
