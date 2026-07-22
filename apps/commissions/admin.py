from django.contrib import admin

from .models import BinaryBonusCycleFailure, BinaryBonusCycleRun


class BinaryBonusCycleFailureInline(admin.TabularInline):
    model = BinaryBonusCycleFailure
    extra = 0
    readonly_fields = ["distributor_id", "error", "created_at"]
    can_delete = False


@admin.register(BinaryBonusCycleRun)
class BinaryBonusCycleRunAdmin(admin.ModelAdmin):
    """System-generated audit trail (apps/commissions/tasks.py::
    calculate_binary_bonus writes these) -- never created, edited, or
    deleted by hand, including by superusers. has_view_permission is left
    at its default, which still resolves True for a superuser via Django's
    own has_perm bypass, so this stays visible without becoming editable."""

    list_display = ["run_at", "evaluated", "paid", "failed", "total_amount"]
    readonly_fields = [
        "run_at",
        "evaluated",
        "paid",
        "failed",
        "total_amount",
        "created_at",
    ]
    inlines = [BinaryBonusCycleFailureInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
