from django.contrib import admin

from .models import Wallet, WalletTransaction


class WalletTransactionInline(admin.TabularInline):
    """Task 15 (2026-07-22): readonly_fields already covers every real
    field, but that's an implicit guarantee -- it would silently stop
    protecting the ledger if a future field were added to
    WalletTransaction and someone forgot to also mark it readonly. These
    three overrides are the explicit, field-independent guarantee instead
    -- credit()/debit() (apps/wallet/services.py) are the ledger's only
    legitimate writers, full stop, mirroring apps/commissions/admin.py's
    CommissionCycleRunAdmin from Task 13 exactly."""

    model = WalletTransaction
    extra = 0
    readonly_fields = ["amount", "transaction_type", "reference", "created_at"]
    ordering = ["-created_at"]

    def has_add_permission(self, request, obj):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    """Read-only visibility for admins -- balance only ever changes via
    apps/wallet/services.py::credit()/debit(), never directly in Django
    Admin, so the balance field itself is not editable here.

    Security review (2026-07-22, Task 15) caught that only the inline
    (WalletTransactionInline) had explicit has_add/change/delete_permission
    overrides -- WalletAdmin itself did not, so a staff/superuser account
    could delete a Wallet row outright via Django Admin's own default
    delete action, cascading (Wallet.CASCADE on WalletTransaction.wallet)
    into silently destroying that distributor's *entire* ledger. Locking
    down all three here closes that, mirroring
    apps/commissions/admin.py::CommissionCycleRunAdmin exactly, the same
    precedent WalletTransactionInline's own docstring already cites."""

    list_display = ["distributor", "balance"]
    search_fields = ["distributor__ir_id"]
    readonly_fields = ["distributor", "balance"]
    inlines = [WalletTransactionInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
