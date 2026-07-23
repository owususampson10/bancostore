from django.contrib import admin, messages
from django.shortcuts import render

from simple_history.admin import SimpleHistoryAdmin

from .models import WithdrawalRequest
from .services import (
    InsufficientWalletBalance,
    KycNotApproved,
    PayoutDestinationNotSet,
    WithdrawalRequestNotFound,
    WithdrawalRequestNotPending,
    approve_withdrawal_request,
    reject_withdrawal_request,
)

# Task 16d: every rejection reason approve/reject can raise, mapped to the
# text an admin sees in the bulk-action results summary. Deliberately one
# dict rather than a growing except-chain per action -- mirrors apps.
# distributors.views._WITHDRAWAL_ERROR_MESSAGES's own reasoning.
#
# code-review-and-quality (2026-07-23): WithdrawalRequestNotFound was
# documented on the service functions but never actually caught here --
# an uncaught exception mid-loop would 500 the whole bulk action, losing
# the results summary for every row already processed in the same
# request (even though each row's own approval/rejection is
# independently atomic and stays committed). Included in both actions'
# except clauses now, not just approve's.
_APPROVE_FAILURE_MESSAGES = {
    KycNotApproved: "KYC is no longer approved",
    PayoutDestinationNotSet: "payout destination is no longer set",
    InsufficientWalletBalance: "wallet balance is now insufficient",
    WithdrawalRequestNotFound: "this request no longer exists",
}


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(SimpleHistoryAdmin):
    """Task 16b/16d. Hard-locked add/change/delete, matching WalletAdmin/
    CommissionCycleRunAdmin exactly (ADR-0004 point 9, corrected 2026-07-22
    after an earlier draft wrongly assumed a non-superuser admin scenario
    that doesn't exist in this project -- every real admin is a superuser,
    per tests/conftest.py::staff_client, and has_view_permission's default
    resolves True for a superuser independent of has_change_permission).
    WithdrawalRequest's only legitimate writers are Task 16c (create) and
    16d/16f (status transitions) -- never a direct admin edit.

    approve_selected/reject_selected each declare permissions=["view"]
    explicitly -- Django's actions default to requiring has_change_
    permission otherwise, which would silently block even a superuser
    given the lockdown above."""

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
    actions = ["approve_selected", "reject_selected"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(
        description="Approve selected withdrawal requests", permissions=["view"]
    )
    def approve_selected(self, request, queryset):
        approved = 0
        failures = []
        # One row's failure must not abort the rest -- both
        # approve_withdrawal_request/reject_withdrawal_request are already
        # individually atomic (Task 16d), so this loop only needs to
        # isolate exceptions, not wrap anything in its own transaction.
        for withdrawal_request in queryset:
            try:
                approve_withdrawal_request(withdrawal_request, reviewed_by=request.user)
                approved += 1
            except WithdrawalRequestNotPending as exc:
                failures.append(f"#{withdrawal_request.pk}: already {exc.status}")
            except tuple(_APPROVE_FAILURE_MESSAGES) as exc:
                failures.append(
                    f"#{withdrawal_request.pk}: "
                    f"{_APPROVE_FAILURE_MESSAGES[type(exc)]}"
                )
        if approved:
            self.message_user(request, f"Approved {approved} withdrawal request(s).")
        for failure in failures:
            self.message_user(
                request, f"Could not approve {failure}.", level=messages.WARNING
            )

    @admin.action(
        description="Reject selected withdrawal requests (with a reason)",
        permissions=["view"],
    )
    def reject_selected(self, request, queryset):
        # Django's documented intermediate-action-page pattern (identical
        # shape to DistributorAdmin.reject_selected_kyc, Task 11): the
        # first pass has no "reason" in POST, so render a form collecting
        # one; the second pass (submitting that form) actually rejects.
        if "reason" in request.POST:
            reason = request.POST["reason"].strip()
            if reason:
                rejected = 0
                failures = []
                for withdrawal_request in queryset:
                    try:
                        reject_withdrawal_request(
                            withdrawal_request, reviewed_by=request.user, reason=reason
                        )
                        rejected += 1
                    except WithdrawalRequestNotPending as exc:
                        failures.append(
                            f"#{withdrawal_request.pk}: already {exc.status}"
                        )
                    except WithdrawalRequestNotFound:
                        failures.append(
                            f"#{withdrawal_request.pk}: this request no longer exists"
                        )
                if rejected:
                    self.message_user(
                        request, f"Rejected {rejected} withdrawal request(s)."
                    )
                for failure in failures:
                    self.message_user(
                        request, f"Could not reject {failure}.", level=messages.WARNING
                    )
                return None

        return render(
            request,
            "admin/withdrawal/reject_withdrawal_confirmation.html",
            {
                "withdrawal_requests": queryset,
                "action_checkbox_name": admin.helpers.ACTION_CHECKBOX_NAME,
                "action": "reject_selected",
            },
        )
