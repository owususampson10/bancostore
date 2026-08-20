from decimal import Decimal

from django import forms
from django.contrib.auth.password_validation import validate_password

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

    def __init__(self, *args, lock_sponsor=False, **kwargs):
        """lock_sponsor renders sponsor_ir_id readonly (not disabled --
        a disabled field's value is never submitted, which would break
        clean_sponsor_ir_id below). This is a UI affordance only: it stops a
        distributor's referred visitor from accidentally clearing the
        pre-filled sponsor ID, it is not a security boundary -- the real
        check is still clean_sponsor_ir_id's DB lookup, which runs
        regardless of this flag."""
        super().__init__(*args, **kwargs)
        if lock_sponsor:
            field = self.fields["sponsor_ir_id"]
            field.widget.attrs["readonly"] = True
            # Swap pr-4 for pr-12, not append alongside it -- the template
            # renders a lock icon over the field's right edge (matching the
            # left-edge "group" icon's own pl-12), and two conflicting pr-*
            # utility classes on one element is undefined which one wins.
            field.widget.attrs["class"] = (
                INPUT_CLASSES.replace("pr-4", "pr-12")
                + " bg-surface-container-low text-on-surface-variant"
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


class PayoutSettingsForm(forms.Form):
    """Task 16a. Both fields required together -- the model's blank=True
    accommodates "not set yet" for a distributor who's never saved anything,
    but a save from this form always sets a complete destination, never a
    partial one (the model's own CheckConstraint is the defense-in-depth
    backstop for any other write path, e.g. Django Admin)."""

    mobile_money_number = PhoneNumberField(
        label="Mobile money number",
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "e.g. 024 123 4567"}
        ),
    )
    mobile_money_network = forms.ChoiceField(
        label="Mobile money network",
        choices=Distributor.MobileMoneyNetwork.choices,
        widget=forms.Select(attrs={"class": INPUT_CLASSES}),
    )


class WithdrawalRequestForm(forms.Form):
    """Task 16c. Deliberately thin -- only checks the amount is a positive,
    2-decimal-place number. MIN_WITHDRAWAL_AMOUNT/MAX_WITHDRAWAL_AMOUNT/
    wallet-balance/KYC/payout-destination/window checks all live in
    apps.withdrawal.services.submit_withdrawal_request, the single source
    of truth for those business rules -- duplicating them here as form
    validators would risk the two drifting apart as MIN/MAX_WITHDRAWAL_
    AMOUNT change live via constance."""

    amount = forms.DecimalField(
        label="Amount to withdraw",
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(
            attrs={
                "class": (
                    "w-full pl-16 pr-5 py-5 bg-surface-container-low rounded-xl "
                    "border-none focus:outline-none focus:ring-2 "
                    "focus:ring-primary font-headline-md text-on-surface "
                    "transition-all"
                ),
                "placeholder": "0.00",
                "step": "0.01",
                # x-model.number, not a separate hidden input, so the tax/net
                # preview stays live off the exact field Django submits --
                # two inputs both named "amount" would silently double-post.
                "x-model.number": "amount",
            }
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
