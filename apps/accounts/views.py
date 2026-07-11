from two_factor.forms import AuthenticationTokenForm, BackupTokenForm
from two_factor.views import LoginView as BaseLoginView

from .forms import AdminAuthenticationForm


class AdminLoginView(BaseLoginView):
    """Same wizard as two_factor's own LoginView, with the 'auth' step's
    form swapped for AdminAuthenticationForm so a locked-out admin gets a
    distinguishable result the login template can render a dedicated
    screen for (see templates/two_factor/core/login.html and
    bancostore/urls.py for how this shadows the packaged login URL)."""

    form_list = (
        (BaseLoginView.AUTH_STEP, AdminAuthenticationForm),
        (BaseLoginView.TOKEN_STEP, AuthenticationTokenForm),
        (BaseLoginView.BACKUP_STEP, BackupTokenForm),
    )

    def get_template_names(self):
        # The 'token'/'backup' steps use their own standalone screen (no
        # shared header/footer) — that's how it was designed in Stitch
        # ("Two-Factor Verification - Bancostore Admin"), unlike the 'auth'
        # step and the rest of the admin screens which share
        # base_admin_auth.html's chrome.
        if self.steps.current in (self.TOKEN_STEP, self.BACKUP_STEP):
            return ["two_factor/core/login_token.html"]
        return [self.template_name]
