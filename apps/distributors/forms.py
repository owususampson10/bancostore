from django import forms
from django.contrib.auth.password_validation import validate_password

from phonenumber_field.formfields import PhoneNumberField

from .models import Distributor


class DistributorRegistrationForm(forms.Form):
    phone_number = PhoneNumberField(label="Phone number")
    email = forms.EmailField(
        label="Email address",
        required=False,
        help_text="For notifications only — not used to log in.",
    )
    password1 = forms.CharField(widget=forms.PasswordInput, label="Password")
    password2 = forms.CharField(widget=forms.PasswordInput, label="Confirm password")

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
    phone_number = PhoneNumberField(label="Phone number")
    password = forms.CharField(widget=forms.PasswordInput, label="Password")


class OTPVerificationForm(forms.Form):
    code = forms.CharField(label="Verification code", min_length=6, max_length=6)


class DistributorForgotPasswordForm(forms.Form):
    phone_number = PhoneNumberField(label="Phone number")


class DistributorSetNewPasswordForm(forms.Form):
    password1 = forms.CharField(widget=forms.PasswordInput, label="New password")
    password2 = forms.CharField(
        widget=forms.PasswordInput, label="Confirm new password"
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
