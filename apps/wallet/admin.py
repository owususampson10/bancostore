from django.contrib import admin

from .models import Wallet, WalletTransaction


class WalletTransactionInline(admin.TabularInline):
    model = WalletTransaction
    extra = 0
    readonly_fields = ["amount", "transaction_type", "reference", "created_at"]
    can_delete = False
    ordering = ["-created_at"]


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    """Read-only visibility for admins -- balance only ever changes via
    apps/wallet/services.py::credit(), never directly in Django Admin, so
    the balance field itself is not editable here."""

    list_display = ["distributor", "balance"]
    search_fields = ["distributor__ir_id"]
    readonly_fields = ["distributor", "balance"]
    inlines = [WalletTransactionInline]
