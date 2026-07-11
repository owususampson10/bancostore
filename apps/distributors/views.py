from django.contrib.auth import get_user_model
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.models import Group
from django.shortcuts import redirect, render

from apps.notifications.otp import generate_otp, verify_otp

from .forms import (
    DistributorForgotPasswordForm,
    DistributorLoginForm,
    DistributorRegistrationForm,
    DistributorSetNewPasswordForm,
    OTPVerificationForm,
)
from .models import Distributor
from .services import attempt_distributor_login

User = get_user_model()

AUTH_BACKEND = "apps.distributors.backends.PhoneNumberBackend"


def register(request):
    if request.method == "POST":
        form = DistributorRegistrationForm(request.POST)
        if form.is_valid():
            phone_number = str(form.cleaned_data["phone_number"])
            user = User.objects.create_user(
                username=phone_number,
                email=form.cleaned_data.get("email", ""),
            )
            user.set_password(form.cleaned_data["password1"])
            user.save()
            Distributor.objects.create(user=user, phone_number=phone_number)
            distributor_group, _ = Group.objects.get_or_create(name="distributor")
            user.groups.add(distributor_group)

            generate_otp(phone_number, purpose="registration")
            request.session["otp_phone_number"] = phone_number
            request.session["otp_purpose"] = "registration"
            return redirect("distributors:verify_otp")
    else:
        form = DistributorRegistrationForm()
    return render(request, "distributors/register.html", {"form": form})


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
                return redirect("distributors:login")
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


def resend_otp(request):
    phone_number = request.session.get("otp_phone_number")
    purpose = request.session.get("otp_purpose")
    if phone_number and purpose:
        generate_otp(phone_number, purpose=purpose)
    return redirect("distributors:verify_otp")


def login_view(request):
    form = DistributorLoginForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        phone_number = str(form.cleaned_data["phone_number"])
        result = attempt_distributor_login(phone_number, form.cleaned_data["password"])

        if result.locked:
            form.add_error(
                None, "Too many failed attempts. Your account is temporarily locked."
            )
        elif result.needs_verification:
            generate_otp(phone_number, purpose="registration")
            request.session["otp_phone_number"] = phone_number
            request.session["otp_purpose"] = "registration"
            return redirect("distributors:verify_otp")
        elif result.success:
            auth_login(request, result.user, backend=AUTH_BACKEND)
            return redirect("distributors:login")
        else:
            form.add_error(None, "Incorrect phone number or password.")

    return render(request, "distributors/login.html", {"form": form})


def logout_view(request):
    auth_logout(request)
    return redirect("distributors:login")


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
        return redirect("distributors:login")

    return render(request, "distributors/set_new_password.html", {"form": form})
