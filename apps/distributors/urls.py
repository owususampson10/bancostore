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
    path(
        "register/pay-fee/callback/",
        views.registration_payment_callback,
        name="registration_payment_callback",
    ),
    path(
        "webhooks/paystack/",
        views.paystack_webhook,
        name="paystack_webhook",
    ),
    path(
        "starter-pack/",
        views.select_starter_pack,
        name="select_starter_pack",
    ),
    path(
        "starter-pack/callback/",
        views.starter_pack_payment_callback,
        name="starter_pack_payment_callback",
    ),
    path("kyc/start/", views.start_kyc_verification, name="start_kyc_verification"),
    path(
        "kyc/callback/",
        views.kyc_verification_callback,
        name="kyc_verification_callback",
    ),
    path("webhooks/didit/", views.didit_webhook, name="didit_webhook"),
    path("verify-otp/", views.verify_otp_view, name="verify_otp"),
    path("verify-otp/resend/", views.resend_otp, name="resend_otp"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("password/forgot/", views.forgot_password, name="forgot_password"),
    path("password/set-new/", views.set_new_password, name="set_new_password"),
    path("password/reset-success/", views.reset_success, name="reset_success"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("earnings-history/", views.earnings_history, name="earnings_history"),
    path("payout-settings/", views.payout_settings, name="payout_settings"),
    path("withdraw/", views.withdrawal_request, name="withdrawal_request"),
    path("withdrawals/", views.withdrawal_history, name="withdrawal_history"),
    path("cancel-membership/", views.cancel_membership, name="cancel_membership"),
]
