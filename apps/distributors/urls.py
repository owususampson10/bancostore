from django.urls import path

from . import views

app_name = "distributors"

urlpatterns = [
    path("register/", views.register, name="register"),
    path("verify-otp/", views.verify_otp_view, name="verify_otp"),
    path("verify-otp/resend/", views.resend_otp, name="resend_otp"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("password/forgot/", views.forgot_password, name="forgot_password"),
    path("password/set-new/", views.set_new_password, name="set_new_password"),
]
