import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views.decorators.http import require_POST
from django.views.defaults import permission_denied as default_permission_denied

from allauth.account.views import (
    PasswordResetDoneView,
    PasswordResetFromKeyDoneView,
    PasswordResetFromKeyView,
    PasswordResetView,
)
from django_otp import devices_for_user
from django_ratelimit.core import is_ratelimited
from two_factor.forms import AuthenticationTokenForm, BackupTokenForm
from two_factor.views import LoginView as BaseLoginView

from .forms import (
    AddressForm,
    AdminAuthenticationForm,
    AdminResetPasswordForm,
    AdminResetPasswordKeyForm,
)
from .models import Address

logger = logging.getLogger(__name__)


def admin_portal_permission_denied(request, exception):
    """Site-wide handler403 (wired in bancostore/urls.py), added to fix a
    known rough edge: apps.admin_portal.permissions.is_admin_portal_staff
    gates every admin_portal view on `user.is_staff and user.is_verified()`
    -- correct for an established admin, but a brand-new admin has zero
    OTP devices at all, so their very first login (nothing to challenge
    against) succeeds while leaving is_verified() False for that session.
    They'd previously land on this project's bare default 403 page with no
    indication that visiting the 2FA setup page themselves is the fix.

    Scoped tightly to avoid becoming an accidental 2FA bypass: only
    redirects when the user is authenticated, is_staff, NOT already
    verified, AND has zero CONFIRMED OTP devices (an unconfirmed
    in-progress device -- someone who started setup but didn't finish --
    still counts as "none", correctly sending them back to finish it). A
    staff user who already has a confirmed device but isn't verified this
    session (e.g. a genuinely stale/tampered session) does NOT match this
    condition and falls through to the normal 403 below -- they need to
    actually complete a real 2FA challenge, not be redirected around it.
    This is a global handler403 rather than a per-view fix because
    apps/admin_portal/views.py repeats `if not is_admin_portal_staff(...):
    raise PermissionDenied` at ~47 separate call sites -- catching the one
    exception centrally is far less risky than editing every call site.

    IMPORTANT: handler403 fires for every PermissionDenied anywhere on the
    site, not just admin_portal -- apps/distributors/views.py and
    apps/catalog/views.py each raise it too, for entirely unrelated
    reasons (not being a distributor, not being eligible to review a
    product). A first version of this fix checked only user state
    (is_staff/is_verified/devices) with no page-scoping at all -- a
    fresh-context adversarial review caught that this would wrongly
    redirect an admin who ALSO happens to hit one of those unrelated
    denials (e.g. browsing a distributor-only page) straight to 2FA
    setup, with a "set up your authenticator app" message that has
    nothing to do with the real reason they were denied. The
    request.resolver_match.app_name check below closes that -- this
    handler only ever fires for the two real admin-access surfaces (the
    branded admin_portal and the raw Django Admin, confirmed via
    django.urls.resolve to have app_name "admin_portal" and "admin"
    respectively), never for a denial on an unrelated page."""
    admin_app_names = {"admin_portal", "admin"}
    on_an_admin_surface = (
        request.resolver_match is not None
        and request.resolver_match.app_name in admin_app_names
    )
    if (
        on_an_admin_surface
        and request.user.is_authenticated
        and request.user.is_staff
        and not request.user.is_verified()
        # devices_for_user is a generator function -- the object it
        # returns is always truthy regardless of whether it yields
        # anything, so `not devices_for_user(...)` would never be True.
        # any(...) actually consumes it to check for a real device.
        and not any(devices_for_user(request.user, confirmed=True))
    ):
        messages.info(
            request,
            "Welcome! Before you can use the admin portal, set up your "
            "authenticator app for two-factor login.",
        )
        return redirect("two_factor:setup")
    return default_permission_denied(request, exception)


class AdminLoginView(BaseLoginView):
    """Same wizard as two_factor's own LoginView, with the 'auth' step's
    form swapped for AdminAuthenticationForm so a locked-out admin gets a
    distinguishable result the login template can render a dedicated
    screen for (see templates/two_factor/core/login.html and
    bancostore/urls.py for how this shadows the packaged login URL).

    Account-level lockout (apps.accounts.forms.AdminAuthenticationForm)
    only protects one email at a time — an attacker can spray guesses
    across many different admin emails from one IP with no limit at all.
    is_ratelimited() is used directly (not the @ratelimit decorator, which
    only wraps function views) to throttle the 'auth' step by IP,
    specifically — a tight limit here, since admin is the highest-value
    target on the platform with the lowest legitimate traffic of the three
    roles (unlike the public register/login endpoints, which need a more
    generous limit for real user volume). The 'token'/'backup' steps are
    NOT throttled the same way: they only matter to a caller who has
    already supplied a correct password for one specific account, so they
    aren't useful for spraying guesses across different admin emails —
    throttling them here too would just risk blocking a legitimate admin
    mistyping their 2FA code a few times in one sitting."""

    form_list = (
        (BaseLoginView.AUTH_STEP, AdminAuthenticationForm),
        (BaseLoginView.TOKEN_STEP, AuthenticationTokenForm),
        (BaseLoginView.BACKUP_STEP, BackupTokenForm),
    )

    def post(self, request, *args, **kwargs):
        is_auth_step = (
            request.POST.get("admin_login_view-current_step") == self.AUTH_STEP
        )
        if is_auth_step and is_ratelimited(
            request,
            group="admin_login_auth_step",
            key="ip",
            rate="5/m",
            method="POST",
            increment=True,
        ):
            return HttpResponse(
                "Too many attempts. Please wait a while and try again.",
                status=429,
            )
        return super().post(request, *args, **kwargs)

    def get_success_url(self):
        # Bug found 2026-07-23 verifying Task 22 live: BaseLoginView.
        # get_success_url() falls back to settings.LOGIN_REDIRECT_URL when
        # there's no safe `next` param, and that setting was never set --
        # every real admin login (not just this project's automated
        # checks) landed on Django's default /accounts/profile/, a 404.
        # LOGIN_REDIRECT_URL itself stays unset deliberately -- it's a
        # single global setting shared by all three roles (one User model
        # per SPEC.md), and distributor login already redirects via its
        # own explicit redirect("distributors:dashboard") calls, not this
        # setting; setting it globally would send a customer or
        # distributor's login flow through whatever value fits admin.
        # Overriding get_success_url() here instead scopes the fix to
        # admin login only.
        #
        # First fixed this to land on Django Admin's own index (/admin/)
        # since apps.admin_portal had no dashboard home yet. That was
        # wrong in its own way -- the user caught it live: they saw the
        # raw unstyled Django backend for a few seconds after every login
        # before reaching the styled KYC review screen, which defeats the
        # entire point of this initiative (the admin should never feel
        # like they've left the branded application). Now lands on
        # apps.admin_portal's own dashboard instead.
        return self.get_redirect_url() or reverse("admin_portal:dashboard")

    def get_template_names(self):
        # The 'token'/'backup' steps use their own standalone screen (no
        # shared header/footer) — that's how it was designed in Stitch
        # ("Two-Factor Verification - Bancostore Admin"), unlike the 'auth'
        # step and the rest of the admin screens which share
        # base_admin_auth.html's chrome.
        if self.steps.current in (self.TOKEN_STEP, self.BACKUP_STEP):
            return ["two_factor/core/login_token.html"]
        return [self.template_name]

    def get_form(self, step=None, data=None, files=None):
        # Security finding (code-review pass, 2026-07-30): "remember this
        # device" must default to UNCHECKED -- admin is the highest-value
        # account type in this system (approves real money withdrawals),
        # so a pre-checked 7-day 2FA skip would be an opt-out, not the
        # opt-in this feature is meant to be.
        #
        # This can't be fixed by subclassing AuthenticationTokenForm and
        # swapping it into form_list -- confirmed via a doubt-driven-
        # development-style investigation that two_factor's own
        # LoginView.get_form() unconditionally overwrites
        # self.form_list[self.TOKEN_STEP] with
        # registry.method_from_device(...).get_token_form_class() on
        # every single call for the token step. self.form_list is the
        # SAME shared OrderedDict object across every request for the
        # whole process lifetime (frozen once by formtools' as_view(),
        # never copied per-request), so that overwrite permanently
        # replaces whatever form class is configured here with the
        # library's own AuthenticationTokenForm the first time any
        # token-step form is built, for every subsequent admin login
        # until the process restarts. Mutating the already-constructed
        # form INSTANCE here instead sidesteps that entirely -- it works
        # no matter which form class the library decided to use.
        form = super().get_form(step=step, data=data, files=files)
        if "remember" in form.fields:
            form.fields["remember"].initial = False
        return form

    def done(self, form_list, **kwargs):
        # Audit trail for "remember this device" (doubt-driven-development
        # finding, 2026-07-30): self.remember_agent (BaseLoginView's own
        # cached_property) is only ever True for a device that has ALREADY
        # completed a real TOTP proof once and was explicitly opted into
        # being remembered -- this log line does not gate anything, it
        # just distinguishes that path from a fresh code entry, matching
        # this codebase's existing convention of auditing security-
        # relevant admin events (KYC/IR ID history, commission cycle
        # runs). Read before delegating to super().done() purely to keep
        # this check next to the condition it logs about; self.get_user()
        # is a memoized self.user_cache, stable across the call either way.
        if self.remember_agent:
            logger.info(
                "Admin login for user_id=%s completed via a remembered "
                "device (no fresh TOTP prompt)",
                self.get_user().pk,
            )
        return super().done(form_list, **kwargs)


class AdminPasswordResetView(PasswordResetView):
    """Admin-branded counterpart of allauth's account_reset_password (the
    admin login page's own "Forgot password?" link points here). Reuses
    ResetPasswordView/ResetPasswordForm's email-lookup, token, and rate-
    limiting logic untouched -- only the template and the emailed link's
    target (via AdminResetPasswordForm) differ, per
    docs/decisions/0012-admin-password-reset-design.md."""

    template_name = "account/admin_password_reset.html"
    form_class = AdminResetPasswordForm
    success_url = reverse_lazy("admin_password_reset_done")

    def get_form_class(self):
        # PasswordResetView.get_form_class() normally does
        # get_form_class(app_settings.FORMS, "reset_password", self.form_class)
        # -- app_settings.FORMS is this project's own global ACCOUNT_FORMS
        # setting (settings.py), which already has a "reset_password" key
        # (CustomerResetPasswordForm) and so silently wins over whatever
        # form_class this subclass sets, regardless of it. Bypassing that
        # lookup entirely is the only way this view's own form_class
        # actually gets used (found by a failing test, not assumed).
        return self.form_class


class AdminPasswordResetDoneView(PasswordResetDoneView):
    template_name = "account/admin_password_reset_done.html"


class AdminPasswordResetFromKeyView(PasswordResetFromKeyView):
    template_name = "account/admin_password_reset_from_key.html"
    form_class = AdminResetPasswordKeyForm
    success_url = reverse_lazy("admin_password_reset_from_key_done")

    def get_form_class(self):
        # Same global-ACCOUNT_FORMS-wins-over-subclass issue as
        # AdminPasswordResetView.get_form_class() above, for the
        # "reset_password_from_key" form id.
        return self.form_class


class AdminPasswordResetFromKeyDoneView(PasswordResetFromKeyDoneView):
    template_name = "account/admin_password_reset_from_key_done.html"


@login_required(login_url="account_login")
def address_list(request):
    """Task 40a. request.user-scoped, no id/param ever accepted -- matching
    apps.distributors.views.earnings_history/team's established
    no-IDOR-surface convention."""
    addresses = Address.objects.filter(user=request.user)
    return render(request, "accounts/address_list.html", {"addresses": addresses})


# Task 40a (fixed 2026-08-12): sourced from the model field's own choices
# (set from apps.orders.models.Order.DeliveryZone at class-definition time)
# rather than re-importing Order here -- one less import edge to audit for
# the accounts->orders cycle risk Address's own docstring already flags.
_DELIVERY_ZONE_CHOICES = [
    {"value": value, "label": label}
    for value, label in Address._meta.get_field("delivery_zone").choices
]
_VALID_DELIVERY_ZONES = {choice["value"] for choice in _DELIVERY_ZONE_CHOICES}


def _selected_delivery_zone(form):
    """Same constrain-to-known-choices-before-the-template reasoning as
    apps.orders.views.checkout_view's own selected_delivery_zone -- the
    Alpine listbox's initial value is threaded through a json_script block
    (never interpolated raw), but the value itself still has to be a real
    choice, not an arbitrary bound-but-invalid submission echoed back."""
    submitted = form.data.get("delivery_zone") if form.is_bound else ""
    if submitted in _VALID_DELIVERY_ZONES:
        return submitted
    initial = form.initial.get("delivery_zone", "")
    return initial if initial in _VALID_DELIVERY_ZONES else ""


@login_required(login_url="account_login")
def address_create(request):
    if request.method == "POST":
        form = AddressForm(request.POST)
        if form.is_valid():
            address = form.save(commit=False)
            address.user = request.user
            address.save()
            messages.success(request, "Address saved.")
            return redirect("accounts:address_list")
    else:
        form = AddressForm()

    return render(
        request,
        "accounts/address_form.html",
        {
            "form": form,
            "is_edit": False,
            "delivery_zone_choices": _DELIVERY_ZONE_CHOICES,
            "selected_delivery_zone": _selected_delivery_zone(form),
        },
    )


@login_required(login_url="account_login")
def address_edit(request, pk):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    if request.method == "POST":
        form = AddressForm(request.POST, instance=address)
        if form.is_valid():
            form.save()
            messages.success(request, "Address updated.")
            return redirect("accounts:address_list")
    else:
        form = AddressForm(instance=address)

    return render(
        request,
        "accounts/address_form.html",
        {
            "form": form,
            "is_edit": True,
            "address": address,
            "delivery_zone_choices": _DELIVERY_ZONE_CHOICES,
            "selected_delivery_zone": _selected_delivery_zone(form),
        },
    )


@login_required(login_url="account_login")
@require_POST
def address_delete(request, pk):
    address = get_object_or_404(Address, pk=pk, user=request.user)
    address.delete()
    messages.success(request, "Address removed.")
    return redirect("accounts:address_list")
