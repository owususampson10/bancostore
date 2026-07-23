from django.http import HttpResponse
from django.urls import reverse

from django_ratelimit.core import is_ratelimited
from two_factor.forms import AuthenticationTokenForm, BackupTokenForm
from two_factor.views import LoginView as BaseLoginView

from .forms import AdminAuthenticationForm


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
