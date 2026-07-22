from django.contrib import admin

from .models import CommissionCycleFailure, CommissionCycleRun


class CommissionCycleFailureInline(admin.TabularInline):
    model = CommissionCycleFailure
    extra = 0
    readonly_fields = ["distributor_id", "error", "created_at"]
    can_delete = False


@admin.register(CommissionCycleRun)
class CommissionCycleRunAdmin(admin.ModelAdmin):
    """System-generated audit trail (each commission batch-driver task in
    apps/commissions/tasks.py writes these, discriminated by job_name) --
    never created, edited, or deleted by hand, including by superusers.
    has_view_permission is left at its default, which still resolves True
    for a superuser via Django's own has_perm bypass, so this stays
    visible without becoming editable."""

    list_display = ["job_name", "run_at", "evaluated", "paid", "failed", "total_amount"]
    list_filter = ["job_name"]
    readonly_fields = [
        "job_name",
        "run_at",
        "evaluated",
        "paid",
        "failed",
        "total_amount",
        "created_at",
    ]
    inlines = [CommissionCycleFailureInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
