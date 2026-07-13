import json
import logging
import math

from django.contrib.auth import get_user_model
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import make_password
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from django_ratelimit.decorators import ratelimit

from apps.notifications.otp import generate_otp, verify_otp

from .forms import (
    DistributorForgotPasswordForm,
    DistributorLoginForm,
    DistributorRegistrationForm,
    DistributorSetNewPasswordForm,
    OTPVerificationForm,
)
from .models import Distributor, PendingRegistration
from .paystack import PaystackError, initialize_transaction, verify_webhook_signature
from .services import (
    PendingRegistrationAlreadyConsumed,
    PendingRegistrationNotFound,
    attempt_distributor_login,
    consume_paid_registration,
    snapshot_payment_reference,
)

User = get_user_model()
logger = logging.getLogger(__name__)

AUTH_BACKEND = "apps.distributors.backends.PhoneNumberBackend"


@ratelimit(key="ip", rate="5/h", method="POST")
def register(request):
    """Task 10a: creates a PendingRegistration, not a live account -- the
    real User/Distributor is only created once the registration fee is
    confirmed paid (Task 10b, not built yet). See tests/feature/
    distributors/test_registration_pending.py and the doubt-driven-development
    design note in apps/distributors/models.py::PendingRegistration."""
    if request.method == "POST":
        form = DistributorRegistrationForm(request.POST)
        if form.is_valid():
            pending = PendingRegistration.objects.create(
                full_name=form.cleaned_data["full_name"],
                phone_number=str(form.cleaned_data["phone_number"]),
                email=form.cleaned_data.get("email", ""),
                address=form.cleaned_data["address"],
                area=form.cleaned_data["area"],
                landmark=form.cleaned_data.get("landmark", ""),
                password_hash=make_password(form.cleaned_data["password1"]),
                sponsor=form.cleaned_data["sponsor"],
            )
            request.session["pending_registration_token"] = str(pending.token)
            return redirect("distributors:pay_registration_fee")
    else:
        form = DistributorRegistrationForm(
            initial={"sponsor_ir_id": request.GET.get("ref", "")}
        )
    return render(request, "distributors/register.html", {"form": form})


@ratelimit(key="ip", rate="20/h", method="GET")
def pay_registration_fee(request):
    """Task 10b: initializes a Paystack transaction for the registration
    fee and redirects to the hosted checkout. See
    project_paystack_registration_payment_design (doubt-driven-development,
    2026-07-13) for why the fee and Paystack reference are snapshotted onto
    PendingRegistration here rather than re-derived later."""
    token = request.session.get("pending_registration_token")
    if not token:
        return redirect("distributors:register")

    try:
        pending = snapshot_payment_reference(token)
    except PendingRegistrationNotFound:
        return redirect("distributors:register")
    except PendingRegistrationAlreadyConsumed:
        return redirect("distributors:login")

    callback_url = request.build_absolute_uri(
        reverse("distributors:registration_payment_callback")
    )
    # Paystack requires an email on every transaction; the form's email
    # field is optional (SPEC.md Section 4 lists it as "for notifications
    # only"), so fall back to a synthetic address that satisfies the API
    # without claiming it's a real contact channel.
    email = pending.email or f"{pending.phone_number}@bancostore.test"

    try:
        data = initialize_transaction(
            email=email,
            amount_pesewas=pending.fee_amount_pesewas,
            reference=pending.payment_reference,
            callback_url=callback_url,
        )
    except PaystackError:
        logger.exception(
            "pay_registration_fee: Paystack initialize_transaction failed "
            "for token=%s",
            token,
        )
        return render(
            request, "distributors/pay_registration_fee.html", {"error": True}
        )

    return redirect(data["authorization_url"])


def registration_payment_callback(request):
    """The user's browser lands here after attempting payment on Paystack's
    hosted checkout. NOT the source of truth for account creation (the
    webhook is) -- but calls the same idempotent consume function as a
    fast-path, since Paystack's own redirect already carries the reference
    as a query param. See project_paystack_registration_payment_design."""
    reference = request.GET.get("reference", "")
    if reference:
        consume_paid_registration(reference)
    return render(request, "distributors/registration_payment_callback.html")


@csrf_exempt
@require_POST
def paystack_webhook(request):
    """Source: https://paystack.com/docs/payments/webhooks/ -- must verify
    the x-paystack-signature header before acting, and must return 200 OK
    or Paystack retries the same event for up to 72 hours (so this handler
    must be idempotent, which consume_paid_registration is)."""
    signature = request.headers.get("x-paystack-signature", "")
    if not verify_webhook_signature(request.body, signature):
        return HttpResponseBadRequest("invalid signature")

    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest("invalid JSON")

    if payload.get("event") == "charge.success":
        reference = payload.get("data", {}).get("reference", "")
        if reference:
            consume_paid_registration(reference)

    return HttpResponse(status=200)


@ratelimit(key="ip", rate="10/m", method="POST")
def verify_otp_view(request):
    phone_number = request.session.get("otp_phone_number")
    purpose = request.session.get("otp_purpose")
    if not phone_number or not purpose:
        return redirect("distributors:register")

    form = OTPVerificationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if verify_otp(
            phone_number, purpose=purpose, submitted_code=form.cleaned_data["code"]
        ):
            del request.session["otp_phone_number"]
            del request.session["otp_purpose"]
            if purpose == "registration":
                distributor = Distributor.objects.select_related("user").get(
                    phone_number=phone_number
                )
                distributor.phone_verified = True
                distributor.save(update_fields=["phone_verified"])
                auth_login(request, distributor.user, backend=AUTH_BACKEND)
                return redirect("distributors:dashboard")
            # password_reset: hold the verified phone number for the next
            # step, but don't log the user in yet — they haven't set a new
            # password.
            request.session["reset_verified_phone_number"] = phone_number
            return redirect("distributors:set_new_password")
        form.add_error("code", "Incorrect or expired code.")

    return render(
        request,
        "distributors/verify_otp.html",
        {"form": form, "phone_number": phone_number},
    )


@require_POST
@ratelimit(key="ip", rate="5/h", method="POST")
def resend_otp(request):
    # require_POST closes a real gap: this view has a side effect (a real,
    # billed SMS send) but previously accepted any method — a bare GET
    # bypasses Django's CSRF check entirely (CSRF only applies to
    # state-changing methods), so an <img> tag or similar could trigger a
    # send while a victim had an in-progress OTP flow.
    phone_number = request.session.get("otp_phone_number")
    purpose = request.session.get("otp_purpose")
    if phone_number and purpose:
        generate_otp(phone_number, purpose=purpose)
    return redirect("distributors:verify_otp")


@ratelimit(key="ip", rate="20/m", method="POST")
def login_view(request):
    form = DistributorLoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        phone_number = str(form.cleaned_data["phone_number"])
        result = attempt_distributor_login(phone_number, form.cleaned_data["password"])

        if result.locked:
            seconds_remaining = (result.locked_until - timezone.now()).total_seconds()
            minutes_remaining = max(1, math.ceil(seconds_remaining / 60))
            return render(
                request,
                "distributors/account_locked.html",
                {"minutes_remaining": minutes_remaining},
            )
        elif result.needs_verification:
            generate_otp(phone_number, purpose="registration")
            request.session["otp_phone_number"] = phone_number
            request.session["otp_purpose"] = "registration"
            return redirect("distributors:verify_otp")
        elif result.success:
            auth_login(request, result.user, backend=AUTH_BACKEND)
            return redirect("distributors:dashboard")
        else:
            form.add_error(None, "Incorrect phone number or password.")

    return render(request, "distributors/login.html", {"form": form})


def logout_view(request):
    auth_logout(request)
    return redirect("distributors:login")


@ratelimit(key="ip", rate="5/h", method="POST")
def forgot_password(request):
    form = DistributorForgotPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        phone_number = str(form.cleaned_data["phone_number"])
        if Distributor.objects.filter(phone_number=phone_number).exists():
            generate_otp(phone_number, purpose="password_reset")
        # Redirect the same way regardless of whether the account exists,
        # to avoid revealing which phone numbers are registered.
        request.session["otp_phone_number"] = phone_number
        request.session["otp_purpose"] = "password_reset"
        return redirect("distributors:verify_otp")
    return render(request, "distributors/forgot_password.html", {"form": form})


def set_new_password(request):
    phone_number = request.session.get("reset_verified_phone_number")
    if not phone_number:
        return redirect("distributors:forgot_password")

    form = DistributorSetNewPasswordForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        distributor = Distributor.objects.select_related("user").get(
            phone_number=phone_number
        )
        distributor.user.set_password(form.cleaned_data["password1"])
        distributor.user.save()
        del request.session["reset_verified_phone_number"]
        return redirect("distributors:reset_success")

    return render(request, "distributors/set_new_password.html", {"form": form})


def reset_success(request):
    return render(request, "distributors/reset_success.html")


@login_required(login_url="distributors:login")
def dashboard(request):
    """Placeholder landing page after a successful login/registration — the
    real dashboard is Task 20. Exists so login has somewhere honest to send
    a distributor, instead of back to the login page itself (which looked
    exactly like the login had silently failed)."""
    return render(request, "distributors/dashboard.html")
