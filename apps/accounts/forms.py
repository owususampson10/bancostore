from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.utils import timezone

from allauth.account.forms import (
    LoginForm,
    ResetPasswordForm,
    ResetPasswordKeyForm,
    SignupForm,
)
from phonenumber_field.formfields import PhoneNumberField

from .models import AdminProfile, CustomerProfile

# Shared Tailwind classes matching the Bancostore Stitch design system
# (see static/src/main.css @theme and templates/account/*.html).
INPUT_CLASSES = (
    "w-full pl-10 pr-4 py-3 bg-white border border-outline-variant rounded-lg "
    "font-body-md text-body-md focus:ring-2 focus:ring-secondary "
    "focus:border-secondary outline-none transition-all"
)
CHECKBOX_CLASSES = (
    "w-4 h-4 rounded border-outline-variant text-primary focus:ring-primary"
)


class CustomerSignupForm(SignupForm):
    """ACCOUNT_USERNAME_REQUIRED=False already makes the base form drop the
    username field itself, so there's no need to remove it here too."""

    full_name = forms.CharField(
        max_length=150,
        label="Full name",
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "John Doe"}
        ),
    )
    phone_number = PhoneNumberField(
        label="Phone number",
        widget=forms.TextInput(
            attrs={"class": INPUT_CLASSES, "placeholder": "+233 XX XXX XXXX"}
        ),
    )
    terms_accepted = forms.BooleanField(
        required=True,
        label="I agree to the Terms of Service and Privacy Policy.",
        error_messages={
            "required": "You must agree to the Terms of Service and Privacy Policy."
        },
        widget=forms.CheckboxInput(attrs={"class": CHECKBOX_CLASSES}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].widget.attrs["class"] = INPUT_CLASSES
        self.fields["password1"].widget.attrs["class"] = INPUT_CLASSES
        self.fields["password2"].widget.attrs["class"] = INPUT_CLASSES

    def save(self, request):
        user = super().save(request)
        CustomerProfile.objects.create(
            user=user,
            full_name=self.cleaned_data["full_name"],
            phone_number=self.cleaned_data["phone_number"],
        )
        customer_group, _ = Group.objects.get_or_create(name="customer")
        user.groups.add(customer_group)
        return user


class CustomerLoginForm(LoginForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["login"].widget.attrs["class"] = INPUT_CLASSES
        self.fields["password"].widget.attrs["class"] = INPUT_CLASSES
        # The template renders its own "Forgot password?" link next to the
        # label, matching the Stitch design, so drop allauth's auto help_text
        # link to avoid showing it twice.
        self.fields["password"].help_text = ""
        if "remember" in self.fields:
            self.fields["remember"].widget.attrs["class"] = CHECKBOX_CLASSES


class CustomerResetPasswordForm(ResetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].widget.attrs["class"] = INPUT_CLASSES


class CustomerResetPasswordKeyForm(ResetPasswordKeyForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].widget.attrs["class"] = INPUT_CLASSES
        self.fields["password2"].widget.attrs["class"] = INPUT_CLASSES


# Matches the admin/2FA Stitch screens' input styling (templates/base_admin_auth.html).
ADMIN_INPUT_CLASSES = (
    "w-full pl-10 pr-4 py-3 bg-surface rounded-lg border border-outline-variant "
    "focus:border-secondary focus:ring-1 focus:ring-secondary transition-all "
    "outline-none font-body-md"
)


class AdminAuthenticationForm(AuthenticationForm):
    """The 'auth' step form for two_factor's login wizard (see
    apps.accounts.views.AdminLoginView). Django's stock AuthenticationForm
    can't tell a locked-out account apart from a plain wrong password —
    authenticate() swallows the PermissionDenied that apps.accounts.backends.
    EmailBackend raises and just returns None either way. This form looks up
    the lockout state directly so the login template can render the
    dedicated Account Locked screen instead of a generic error."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.locked = False
        self.fields["username"].label = "Email Address"
        self.fields["username"].widget.attrs.update(
            {"class": ADMIN_INPUT_CLASSES, "placeholder": "name@bancostore.internal"}
        )
        self.fields["password"].widget.attrs.update(
            {"class": ADMIN_INPUT_CLASSES, "placeholder": "••••••••"}
        )

    def clean(self):
        email = self.cleaned_data.get("username")
        if email:
            self.locked = AdminProfile.objects.filter(
                user__email__iexact=email,
                user__is_staff=True,
                locked_until__gt=timezone.now(),
            ).exists()
            if self.locked:
                raise ValidationError("Account temporarily locked.")
        return super().clean()
