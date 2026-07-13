from django.contrib import admin

from .models import BinaryTreeEdge


@admin.register(BinaryTreeEdge)
class BinaryTreeEdgeAdmin(admin.ModelAdmin):
    list_display = ["ancestor", "descendant", "depth", "leg"]
    list_filter = ["leg", "depth"]
    search_fields = ["ancestor__ir_id", "descendant__ir_id"]
