from django.contrib import admin

from simple_history.admin import SimpleHistoryAdmin

from .models import Category, Product, ProductImage, ProductVariant


@admin.register(Category)
class CategoryAdmin(SimpleHistoryAdmin):
    list_display = ["name", "slug"]
    prepopulated_fields = {"slug": ("name",)}


class ProductImageInline(admin.TabularInline):
    model = ProductImage
    extra = 1


class ProductVariantInline(admin.TabularInline):
    model = ProductVariant
    extra = 1


@admin.register(Product)
class ProductAdmin(SimpleHistoryAdmin):
    list_display = [
        "name",
        "category",
        "price",
        "pv_value",
        "stock",
        "is_active",
        "is_featured",
    ]
    list_filter = ["category", "is_active", "is_featured"]
    search_fields = ["name", "description"]
    prepopulated_fields = {"slug": ("name",)}
    inlines = [ProductImageInline, ProductVariantInline]
    # Not required — Django's ChangeList already auto-applies select_related()
    # when a relation field appears in list_display and list_select_related
    # is left at its False default (see apply_select_related() /
    # has_related_field_in_list_display() in django.contrib.admin.views.main).
    # Kept explicit anyway so the query behavior doesn't depend on a reader
    # knowing that fallback exists.
    list_select_related = ["category"]
