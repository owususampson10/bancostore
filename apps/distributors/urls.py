from django.urls import path

from . import views

app_name = "distributors"

urlpatterns = [
    path("register/", views.register, name="register"),
    path(
        "register/pay-fee/",
        views.pay_registration_fee,
        name="pay_registration_fee",
    ),
    path("verify-otp/", views.verify_otp_view, name="verify_otp"),
    path("verify-otp/resend/", views.resend_otp, name="resend_otp"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("password/forgot/", views.forgot_password, name="forgot_password"),
    path("password/set-new/", views.set_new_password, name="set_new_password"),
    path("password/reset-success/", views.reset_success, name="reset_success"),
    path("dashboard/", views.dashboard, name="dashboard"),
]
