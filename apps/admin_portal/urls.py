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
]
