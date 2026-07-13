from django.contrib import admin

from .models import PvLedger


@admin.register(PvLedger)
class PvLedgerAdmin(admin.ModelAdmin):
    list_display = ["distributor", "left_leg_pv", "right_leg_pv"]
    search_fields = ["distributor__ir_id"]
