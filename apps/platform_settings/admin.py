from django.contrib import admin
from django.core.exceptions import ValidationError

from constance.admin import Config, ConstanceAdmin, ConstanceForm

from .models import PlatformSettingChange


def validate_withdrawal_amount_bounds(cleaned_data):
    """Cross-field check constance's own form doesn't provide -- each
    setting is validated independently (both MIN/MAX_WITHDRAWAL_AMOUNT
    only need to be >= 0 on their own, via non_negative_money_field), so
    nothing stopped an admin from saving a minimum above the maximum,
    which would make every withdrawal request permanently unsatisfiable
    once Task 16 ships (no amount can be both >= a too-high minimum and
    <= a too-low maximum). Caught by CodeRabbit review, 2026-07-22."""
    min_amount = cleaned_data.get("MIN_WITHDRAWAL_AMOUNT")
    max_amount = cleaned_data.get("MAX_WITHDRAWAL_AMOUNT")
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        raise ValidationError(
            "MIN_WITHDRAWAL_AMOUNT (%(min)s) cannot be greater than "
            "MAX_WITHDRAWAL_AMOUNT (%(max)s).",
            params={"min": min_amount, "max": max_amount},
        )


class BancostoreConstanceForm(ConstanceForm):
    def clean(self):
        cleaned_data = super().clean()
        validate_withdrawal_amount_bounds(cleaned_data)
        return cleaned_data


class BancostoreConstanceAdmin(ConstanceAdmin):
    """constance's own ConstanceAdmin documents change_list_form as its
    supported extension point (get_changelist_form defaults to returning
    self.change_list_form) -- this is that hook, not a workaround."""

    change_list_form = BancostoreConstanceForm


admin.site.unregister([Config])
admin.site.register([Config], BancostoreConstanceAdmin)


@admin.register(PlatformSettingChange)
class PlatformSettingChangeAdmin(admin.ModelAdmin):
    """Task 47d. Read-only audit trail, hard-locked from the first
    commit -- record_setting_change (apps/platform_settings/signals.py)
    is this table's only legitimate writer, matching every other
    audit-trail admin registration in this codebase (WalletAdmin,
    CommissionCycleRunAdmin, EscrowLedgerAdmin, ...)."""

    list_display = ["key", "old_value", "new_value", "changed_by", "changed_at"]
    list_filter = ["key"]
    search_fields = ["key"]
    readonly_fields = ["key", "old_value", "new_value", "changed_by", "changed_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
