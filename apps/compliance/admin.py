from django.contrib import admin

from .models import EscrowLedger, EscrowTransaction


class EscrowTransactionInline(admin.TabularInline):
    """Task 47a. Hard-locked, mirroring
    apps.wallet.admin.WalletTransactionInline exactly -- credit_escrow()/
    reverse_escrow() (apps/compliance/services.py) are this ledger's only
    legitimate writers, full stop."""

    model = EscrowTransaction
    extra = 0
    readonly_fields = [
        "order_id",
        "transaction_type",
        "amount",
        "rate_applied",
        "created_at",
    ]
    ordering = ["-created_at"]

    def has_add_permission(self, request, obj):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(EscrowLedger)
class EscrowLedgerAdmin(admin.ModelAdmin):
    """Read-only visibility for admins -- balance only ever changes via
    apps/compliance/services.py::credit_escrow()/reverse_escrow(), never
    directly in Django Admin. Hard-locked on all three permissions from
    the first commit, not added reactively after a review -- this exact
    codebase already shipped the "only the inline was locked down"
    version of this gap once (WalletAdmin, Task 15), caught only by a
    follow-up security-and-hardening pass; a doubt-driven-development
    review before this feature's own implementation flagged the same
    gap here explicitly so it doesn't ship a second time."""

    list_display = ["pk", "balance", "updated_at"]
    readonly_fields = ["balance", "created_at", "updated_at"]
    inlines = [EscrowTransactionInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
