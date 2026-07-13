from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from phonenumber_field.formfields import PhoneNumberField

from .models import Distributor, PendingRegistration

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

# Task 11a: KYC is this project's first file-upload endpoint reachable by a
# non-admin, untrusted actor (Category/ProductImage uploads are admin-only).
# Rejecting oversized files here, before Distributor.save()'s Pillow
# resize/convert step ever runs, keeps a large upload from spending real
# CPU/memory before it's even validated.
MAX_KYC_UPLOAD_SIZE_BYTES = 5 * 1024 * 1024  # 5MB


def _validate_kyc_upload_size(uploaded_file):
    if uploaded_file.size > MAX_KYC_UPLOAD_SIZE_BYTES:
        raise ValidationError("File must be smaller than 5MB.")


class DistributorRegistrationForm(forms.Form):
    """Task 10a extends this with the fields Section 4 step 1 requires
    beyond basic auth mechanics (full name, address, area, landmark,
    sponsor's IR ID). Deliberately does NOT check phone/email against
    existing Distributor rows here (see clean() below) -- enumeration
    prevention confirmed via doubt-driven-development 2026-07-13: the real
    uniqueness check happens later, at account-creation time in Task 10b's
    webhook handler, not in this synchronous, probeable response.
    """

    full_name = forms.CharField(
        label="Full name",
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "Full name"}
        ),
    )
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
    address = forms.CharField(
        label="House address",
        max_length=255,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "House address"}
        ),
    )
    area = forms.CharField(
        label="Area",
        max_length=255,
        widget=forms.TextInput(attrs={"class": INPUT_CLASSES, "placeholder": "Area"}),
    )
    landmark = forms.CharField(
        label="Landmark",
        max_length=255,
        required=False,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "Nearby landmark"}
        ),
    )
    sponsor_ir_id = forms.CharField(
        label="Sponsor's IR ID",
        max_length=50,
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "e.g. IR00245"}
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

    def clean_sponsor_ir_id(self):
        ir_id = self.cleaned_data["sponsor_ir_id"]
        sponsor = Distributor.objects.filter(ir_id=ir_id).first()
        if sponsor is None:
            raise forms.ValidationError("This sponsor IR ID could not be found.")
        self.cleaned_data["sponsor"] = sponsor
        return ir_id

    def clean_phone_number(self):
        # Checking PendingRegistration (not Distributor) here isn't the
        # enumeration channel the class docstring warns about: it only
        # reveals "a signup is already in progress" for this number, never
        # whether it belongs to an actual distributor. Needed regardless,
        # to avoid an unhandled IntegrityError from PendingRegistration's
        # unique phone_number constraint.
        phone_number = self.cleaned_data["phone_number"]
        if PendingRegistration.objects.filter(phone_number=phone_number).exists():
            raise forms.ValidationError(
                "A registration for this phone number is already in progress."
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


class KycSubmissionForm(forms.Form):
    """Task 11a: Ghana Card front/back + selfie. All three are required
    here regardless of the underlying Distributor fields' blank=True --
    that's just so a freshly-created Distributor (who hasn't submitted
    KYC yet) doesn't fail model validation, not a statement that these are
    optional for submission."""

    ghana_card_front = forms.ImageField(
        label="Ghana Card (front)",
        validators=[_validate_kyc_upload_size],
        widget=forms.ClearableFileInput(attrs={"class": INPUT_CLASSES}),
    )
    ghana_card_back = forms.ImageField(
        label="Ghana Card (back)",
        validators=[_validate_kyc_upload_size],
        widget=forms.ClearableFileInput(attrs={"class": INPUT_CLASSES}),
    )
    selfie = forms.ImageField(
        label="Selfie",
        validators=[_validate_kyc_upload_size],
        widget=forms.ClearableFileInput(attrs={"class": INPUT_CLASSES}),
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
