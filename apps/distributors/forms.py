from django import forms
from django.contrib.auth.password_validation import validate_password

from phonenumber_field.formfields import PhoneNumberField

from .models import Distributor

# Shared Tailwind classes matching the Bancostore Stitch design system
# (see static/src/main.css @theme and templates/distributors/*.html).
INPUT_CLASSES = (
    "w-full pl-12 pr-4 py-3 bg-surface border border-outline-variant rounded-lg "
    "font-body-md text-body-md focus:outline-none focus:border-secondary "
    "focus:ring-2 focus:ring-secondary transition-all"
)
CHECKBOX_CLASSES = (
    "mt-1 w-4 h-4 text-primary border-outline-variant rounded focus:ring-primary"
)


class DistributorRegistrationForm(forms.Form):
    phone_number = PhoneNumberField(
        label="Phone number",
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "e.g. 024 123 4567"}
        ),
    )
    email = forms.EmailField(
        label="Email address",
        required=False,
        help_text="For notifications only — not used to log in.",
        widget=forms.EmailInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "you@example.com"}
        ),
    )
    password1 = forms.CharField(
        widget=forms.PasswordInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "Create a secure password"}
        ),
        label="Password",
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "Repeat your password"}
        ),
        label="Confirm password",
    )
    terms_accepted = forms.BooleanField(
        required=True,
        label=(
            "I agree to the Distributor Agreement and authorize Bancostore to send "
            "business updates to my provided contact info."
        ),
        error_messages={"required": "You must agree to the Distributor Agreement."},
        widget=forms.CheckboxInput(attrs={"class": CHECKBOX_CLASSES}),
    )

    def clean_phone_number(self):
        phone_number = self.cleaned_data["phone_number"]
        if Distributor.objects.filter(phone_number=phone_number).exists():
            raise forms.ValidationError(
                "An account with this phone number already exists."
            )
        return phone_number

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("Passwords do not match.")
        if password1:
            validate_password(password1)
        return cleaned_data


class DistributorLoginForm(forms.Form):
    phone_number = PhoneNumberField(
        label="Phone number",
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "024 123 4567"}
        ),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "Enter your password"}
        ),
        label="Password",
    )


class OTPVerificationForm(forms.Form):
    """Rendered in the template as 6 separate digit boxes (matching the Stitch
    design) synced into this single hidden field by a small JS snippet — see
    templates/distributors/verify_otp.html."""

    code = forms.CharField(
        label="Verification code",
        min_length=6,
        max_length=6,
        widget=forms.HiddenInput(attrs={"id": "id_code"}),
    )


class DistributorForgotPasswordForm(forms.Form):
    phone_number = PhoneNumberField(
        label="Phone number",
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "024 123 4567"}
        ),
    )


class DistributorSetNewPasswordForm(forms.Form):
    password1 = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": INPUT_CLASSES}),
        label="New password",
    )
    password2 = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": INPUT_CLASSES}),
        label="Confirm new password",
    )

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get("password1")
        password2 = cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("Passwords do not match.")
        if password1:
            validate_password(password1)
        return cleaned_data
