import logging

from django.http import HttpResponse
from django.urls import reverse

from django_ratelimit.core import is_ratelimited
from two_factor.forms import AuthenticationTokenForm, BackupTokenForm
from two_factor.views import LoginView as BaseLoginView

from .forms import AdminAuthenticationForm

logger = logging.getLogger(__name__)


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
                "Admin login for %s completed via a remembered device "
                "(no fresh TOTP prompt)",
                self.get_user().email,
            )
        return super().done(form_list, **kwargs)
