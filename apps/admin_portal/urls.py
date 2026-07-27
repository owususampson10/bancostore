from django.urls import path

from . import views

app_name = "admin_portal"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("kyc-review/", views.kyc_review_queue, name="kyc_review_queue"),
    path("kyc-review/<int:pk>/", views.kyc_review_detail, name="kyc_review_detail"),
    path("withdrawals/", views.withdrawal_review_queue, name="withdrawal_review_queue"),
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
]
