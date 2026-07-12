from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from .models import AdminProfile

User = get_user_model()


class EmailBackend(ModelBackend):
    """Admin logs in with email + password (SPEC.md Section 2.3), not
    username. django-two-factor-auth's login wizard submits Django's stock
    AuthenticationForm as the "auth" step, which calls authenticate() with a
    `username` kwarg regardless of what the field actually represents — so
    this backend treats that value as an email address instead of a lookup
    against User.username.

    Matches is_staff users only. Note that allauth's own backend (configured
    for email-based customer login, ACCOUNT_AUTHENTICATION_METHOD="email")
    will also happily match a staff user's email+password, since it has no
    concept of "staff-only" or lockout — so a locked-out admin must raise
    PermissionDenied here, not just return None. Django's authenticate()
    stops trying further backends on PermissionDenied but keeps going after
    a plain None, so returning None here would let allauth's backend
    authenticate the very account this is supposed to be blocking.

    The lock check runs AFTER check_password(), not before. Checking lock
    state first would mean a locked account raises PermissionDenied (and
    skips the slow password-hash comparison) for any submitted password,
    while an unlocked account always pays that cost — a measurable timing
    side-channel revealing "this account is currently locked" to a caller
    who doesn't know the real password. This is the same threat model
    apps.accounts.forms.AdminAuthenticationForm.clean() was written to
    close at the message level; this backend needs the same ordering, not
    just the form (see tests/feature/accounts/test_admin_auth.py::
    test_email_backend_does_not_check_lock_state_before_the_password)."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not username or not password:
            return None
        try:
            user = User.objects.get(email__iexact=username, is_staff=True)
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            return None

        if not (user.check_password(password) and self.user_can_authenticate(user)):
            return None

        profile = AdminProfile.objects.filter(user=user).first()
        if profile and profile.locked_until and profile.locked_until > timezone.now():
            raise PermissionDenied("Account temporarily locked.")

        return user
