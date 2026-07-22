from django.contrib import admin

from simple_history.admin import SimpleHistoryAdmin

from .models import WithdrawalRequest


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(SimpleHistoryAdmin):
    """Task 16b. Hard-locked add/change/delete, matching WalletAdmin/
    CommissionCycleRunAdmin exactly (ADR-0004 point 9, corrected 2026-07-22
    after an earlier draft wrongly assumed a non-superuser admin scenario
    that doesn't exist in this project -- every real admin is a superuser,
    per tests/conftest.py::staff_client, and has_view_permission's default
    resolves True for a superuser independent of has_change_permission).
    WithdrawalRequest's only legitimate writers are Task 16c (create) and
    16d/16f (status transitions) -- never a direct admin edit.

    No actions yet -- Task 16d adds approve/reject bulk actions, each of
    which must declare `permissions=["view"]` explicitly (Django's actions
    default to requiring has_change_permission otherwise, which would
    silently block even a superuser given the lockdown below)."""

    list_display = [
        "distributor",
        "amount",
        "tax_amount",
        "net_amount",
        "status",
        "created_at",
    ]
    list_filter = ["status"]
    search_fields = ["distributor__ir_id"]
    readonly_fields = [f.name for f in WithdrawalRequest._meta.fields]
    # code-review-and-quality (2026-07-22): Distributor.__str__ dereferences
    # .user, so without this every changelist row cost 2 extra queries
    # (Distributor, then User) -- select_related both in one join.
    list_select_related = ["distributor", "distributor__user"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
