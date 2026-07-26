from django.contrib import admin

from simple_history.admin import SimpleHistoryAdmin

from .models import Order, OrderCycleFailure, OrderCycleRun, OrderItem


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(SimpleHistoryAdmin):
    """Task 17a. Hard-locked add/change/delete, matching WalletAdmin/
    CommissionCycleRunAdmin/WithdrawalRequestAdmin exactly -- this is a
    read-only audit view until Task 18 builds the real order-management
    actions (status updates, cancel/refund). Order's only legitimate
    writers are 17c (create) and 17d (payment confirmation).

    Task 18 forward-pointer, from a bug this exact codebase already found
    and fixed once (docs/decisions/0004-withdrawal-payout-design.md #9):
    Django admin actions default to requiring has_change_permission, so
    any bulk action Task 18 adds here (e.g. bulk status update) MUST
    declare `permissions=["view"]` explicitly, or the unconditional
    has_change_permission=False below silently blocks it for every admin,
    superuser included -- copy WithdrawalRequestAdmin's
    approve_selected/reject_selected pattern, don't rediscover this."""

    list_display = ["id", "customer", "full_name", "status", "total", "created_at"]
    list_filter = ["status", "delivery_method"]
    search_fields = ["full_name", "phone_number", "payment_reference"]
    readonly_fields = [f.name for f in Order._meta.fields]
    list_select_related = ["customer"]
    inlines = [OrderItemInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class OrderCycleFailureInline(admin.TabularInline):
    model = OrderCycleFailure
    extra = 0
    readonly_fields = ["order_id", "error", "created_at"]
    can_delete = False


@admin.register(OrderCycleRun)
class OrderCycleRunAdmin(admin.ModelAdmin):
    """Task 18d. System-generated audit trail (apps.orders.tasks
    .auto_cancel_unpaid_orders writes these) -- never created, edited, or
    deleted by hand, including by superusers, matching
    CommissionCycleRunAdmin/WithdrawalRequestAdmin's identical lockdown."""

    list_display = ["run_at", "evaluated", "cancelled", "failed"]
    readonly_fields = ["run_at", "evaluated", "cancelled", "failed", "created_at"]
    inlines = [OrderCycleFailureInline]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
