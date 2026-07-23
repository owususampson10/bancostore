from django.urls import path

from . import views

app_name = "admin_portal"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("kyc-review/", views.kyc_review_queue, name="kyc_review_queue"),
    path("kyc-review/<int:pk>/", views.kyc_review_detail, name="kyc_review_detail"),
]
