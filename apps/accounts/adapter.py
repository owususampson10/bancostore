from urllib.parse import quote

from django.urls import reverse

from allauth.account.adapter import DefaultAccountAdapter
from allauth.utils import build_absolute_uri


class BancostoreAccountAdapter(DefaultAccountAdapter):
    def get_reset_password_from_key_url(self, key):
        # Task 55b: replaces AdminResetPasswordForm._send_password_reset_mail,
        # a copy of an allauth private method that django-allauth 65.x
        # silently stopped calling -- ResetPasswordForm.save() now goes
        # straight to flows.password_reset.request_password_reset(), which
        # asks this documented adapter hook for the link instead
        # (allauth/account/adapter.py, "intended to be overridden in case the
        # password reset email needs to be adjusted").
        #
        # The hook is project-wide, so it routes on the URL the reset was
        # requested from: only AdminPasswordResetView's own route gets the
        # admin-branded confirm page (ADR-0012). Every other caller --
        # including a request with no resolver match at all -- falls through
        # to allauth's own customer-facing link, the safe default.
        resolver_match = getattr(self.request, "resolver_match", None)
        if resolver_match is None or resolver_match.url_name != "admin_password_reset":
            return super().get_reset_password_from_key_url(key)

        # Same placeholder-then-quote construction allauth's own
        # flows.password_reset.get_reset_password_from_key_url uses.
        path = reverse(
            "admin_password_reset_from_key",
            kwargs={"uidb36": "UID", "key": "KEY"},
        ).replace("UID-KEY", quote(key))
        return build_absolute_uri(self.request, path)
