from django.contrib import admin

from .models import Distributor


@admin.register(Distributor)
class DistributorAdmin(admin.ModelAdmin):
    list_display = ("user", "rank", "kyc_status", "ir_id", "sponsor")
