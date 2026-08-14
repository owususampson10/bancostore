from django.urls import path

from . import views

app_name = "admin_portal"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("kyc-review/", views.kyc_review_queue, name="kyc_review_queue"),
    path("kyc-review/<int:pk>/", views.kyc_review_detail, name="kyc_review_detail"),
    path("withdrawals/", views.withdrawal_review_queue, name="withdrawal_review_queue"),
    path(
        "withdrawals/export/",
        views.withdrawal_review_export,
        name="withdrawal_review_export",
    ),
    path(
        "withdrawals/<int:pk>/",
        views.withdrawal_review_detail,
        name="withdrawal_review_detail",
    ),
    path("distributors/", views.distributor_directory, name="distributor_directory"),
    path(
        "distributors/export/",
        views.distributor_directory_export,
        name="distributor_directory_export",
    ),
    path(
        "distributors/<int:pk>/",
        views.distributor_profile,
        name="distributor_profile",
    ),
    path("commissions/", views.commission_oversight, name="commission_oversight"),
    path(
        "commissions/<int:pk>/",
        views.commission_cycle_detail,
        name="commission_cycle_detail",
    ),
    path("orders/", views.order_management_queue, name="order_management_queue"),
    path("orders/<int:pk>/", views.order_detail, name="order_detail"),
    path(
        "orders/<int:pk>/action/",
        views.order_management_action,
        name="order_management_action",
    ),
    path(
        "orders/<int:pk>/invoice/",
        views.order_invoice_pdf,
        name="order_invoice_pdf",
    ),
    path(
        "catalog/categories/", views.catalog_category_list, name="catalog_category_list"
    ),
    path(
        "catalog/categories/add/",
        views.catalog_category_create,
        name="catalog_category_create",
    ),
    path(
        "catalog/categories/<int:pk>/edit/",
        views.catalog_category_edit,
        name="catalog_category_edit",
    ),
    path(
        "catalog/categories/<int:pk>/delete/",
        views.catalog_category_delete,
        name="catalog_category_delete",
    ),
    path("catalog/products/", views.catalog_product_list, name="catalog_product_list"),
    path(
        "catalog/products/add/",
        views.catalog_product_create,
        name="catalog_product_create",
    ),
    path(
        "catalog/products/<int:pk>/edit/",
        views.catalog_product_edit,
        name="catalog_product_edit",
    ),
    path(
        "catalog/products/<int:pk>/delete/",
        views.catalog_product_delete,
        name="catalog_product_delete",
    ),
    path(
        "catalog/reviews/", views.review_moderation_list, name="review_moderation_list"
    ),
    path("catalog/banners/", views.catalog_banner_list, name="catalog_banner_list"),
    path(
        "catalog/banners/add/",
        views.catalog_banner_create,
        name="catalog_banner_create",
    ),
    path(
        "catalog/banners/<int:pk>/edit/",
        views.catalog_banner_edit,
        name="catalog_banner_edit",
    ),
    path(
        "catalog/banners/<int:pk>/delete/",
        views.catalog_banner_delete,
        name="catalog_banner_delete",
    ),
    path("discount-codes/", views.discount_code_list, name="discount_code_list"),
    path(
        "discount-codes/add/",
        views.discount_code_create,
        name="discount_code_create",
    ),
    path(
        "discount-codes/<int:pk>/edit/",
        views.discount_code_edit,
        name="discount_code_edit",
    ),
    path(
        "discount-codes/<int:pk>/delete/",
        views.discount_code_delete,
        name="discount_code_delete",
    ),
    path(
        "catalog/reviews/<int:pk>/approve/",
        views.review_approve,
        name="review_approve",
    ),
    path(
        "catalog/reviews/<int:pk>/delete/",
        views.review_delete,
        name="review_delete",
    ),
    path("settings/", views.platform_settings, name="platform_settings"),
    # Task 36c: no standalone page -- Social Links now lives inside the
    # "settings/" page's General Platform Settings tab; these three remain
    # as the mutating endpoints that page's own forms POST to.
    path("social-links/add/", views.social_link_create, name="social_link_create"),
    path(
        "social-links/<int:pk>/edit/",
        views.social_link_update,
        name="social_link_update",
    ),
    path(
        "social-links/<int:pk>/delete/",
        views.social_link_delete,
        name="social_link_delete",
    ),
    path(
        "social-links/detect-platform/",
        views.social_link_detect_platform,
        name="social_link_detect_platform",
    ),
]
