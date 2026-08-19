from urllib.parse import quote

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from allauth.account import app_settings
from allauth.account.adapter import get_adapter
from allauth.account.app_settings import AuthenticationMethod
from allauth.account.forms import (
    LoginForm,
    ResetPasswordForm,
    ResetPasswordKeyForm,
    SignupForm,
    default_token_generator,
)
from allauth.account.utils import user_pk_to_url_str, user_username
from allauth.utils import build_absolute_uri
from phonenumber_field.formfields import PhoneNumberField

from .models import Address, AdminProfile, CustomerProfile

User = get_user_model()

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
    dedicated Account Locked screen instead of a generic error.

    The lock check only runs after confirming the submitted password is
    actually correct. Checking lock state first (regardless of password)
    would let anyone who knows/guesses a staff email confirm it's locked —
    and therefore that it's a real, currently-targeted admin account —
    without ever needing to know the real password."""

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
        password = self.cleaned_data.get("password")
        if email and password:
            user = User.objects.filter(email__iexact=email, is_staff=True).first()
            if user and user.check_password(password):
                self.locked = AdminProfile.objects.filter(
                    user=user, locked_until__gt=timezone.now()
                ).exists()
                if self.locked:
                    raise ValidationError("Account temporarily locked.")
        return super().clean()


class AdminResetPasswordForm(ResetPasswordForm):
    """The admin login page's own "Forgot password?" link (previously dead)
    points here rather than at allauth's customer-facing account_reset_password
    -- same underlying email lookup/token/rate-limit machinery (ResetPasswordForm
    is untouched), styled to match the admin/2FA screens and, critically,
    emailing a link back to this app's own admin-branded confirm screen
    (admin_password_reset_from_key) instead of allauth's customer one."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].widget.attrs["class"] = ADMIN_INPUT_CLASSES

    def clean_email(self):
        # code-review-and-quality finding: ResetPasswordForm.clean_email()
        # alone doesn't check is_staff, so a customer/distributor typing
        # their own email here would still get a fully working reset link
        # -- just one that opens on admin-branded chrome/copy ("protect
        # this admin account") for an account that isn't one. Filtering
        # self.users down to staff-only after the base lookup keeps the
        # exact same non-enumeration response either way (this project's
        # ACCOUNT_PREVENT_ENUMERATION default is True): a non-staff email
        # still lands on the generic "check your email" page, it just
        # never actually receives a real reset link through this
        # admin-branded front door.
        email = super().clean_email()
        self.users = [user for user in self.users if user.is_staff]
        return email

    def _send_password_reset_mail(self, request, email, users, **kwargs):
        # Mirrors ResetPasswordForm._send_password_reset_mail (same token
        # generation, same email template, same username-context branch for
        # non-email-based auth) -- the only real change is the target URL
        # name for the link embedded in the email. allauth's own version
        # hardcodes "account_reset_password_from_key" with no hook to
        # override just the URL name, so this duplicates the ~25 lines
        # rather than the whole form/view (security-auditor finding: an
        # earlier version of this method silently dropped the username
        # branch below, which would only ever have mattered if
        # ACCOUNT_AUTHENTICATION_METHOD stopped being "email").
        token_generator = kwargs.get("token_generator", default_token_generator)
        for user in users:
            temp_key = token_generator.make_token(user)
            uid = user_pk_to_url_str(user)
            key = f"{uid}-{temp_key}"
            path = reverse(
                "admin_password_reset_from_key",
                kwargs={"uidb36": "UID", "key": "KEY"},
            ).replace("UID-KEY", quote(key))
            url = build_absolute_uri(request, path)
            context = {
                "user": user,
                "password_reset_url": url,
                "uid": uid,
                "key": temp_key,
                "request": request,
            }
            if app_settings.AUTHENTICATION_METHOD != AuthenticationMethod.EMAIL:
                context["username"] = user_username(user)
            get_adapter().send_mail("account/email/password_reset_key", email, context)


class AdminResetPasswordKeyForm(ResetPasswordKeyForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].widget.attrs["class"] = ADMIN_INPUT_CLASSES
        self.fields["password2"].widget.attrs["class"] = ADMIN_INPUT_CLASSES


class AddressForm(forms.ModelForm):
    """Task 40a (fixed 2026-08-12 per direct user feedback). delivery_zone
    is a HiddenInput here, not a native <select> -- the visible control is
    a themed Alpine listbox rendered entirely in the template
    (templates/accounts/address_form.html), matching checkout.html's own
    Delivery Zone control and payout_settings.html's mobile-money-network
    listbox exactly, since a native select's open options popup can't be
    restyled via CSS in any browser (this codebase's established reason
    for every other themed dropdown)."""

    class Meta:
        model = Address
        fields = ["label", "delivery_zone", "address", "area", "landmark", "is_default"]
        widgets = {
            "label": forms.TextInput(
                attrs={"class": INPUT_CLASSES, "placeholder": "Home, Office..."}
            ),
            "delivery_zone": forms.HiddenInput(),
            "address": forms.TextInput(
                attrs={"class": INPUT_CLASSES, "placeholder": "Street address"}
            ),
            "area": forms.TextInput(
                attrs={"class": INPUT_CLASSES, "placeholder": "Area/suburb"}
            ),
            "landmark": forms.TextInput(
                attrs={
                    "class": INPUT_CLASSES,
                    "placeholder": "Nearest landmark (optional)",
                }
            ),
            "is_default": forms.CheckboxInput(attrs={"class": CHECKBOX_CLASSES}),
        }
