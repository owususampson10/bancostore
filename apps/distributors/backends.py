from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend

from .models import Distributor

User = get_user_model()


class PhoneNumberBackend(ModelBackend):
    """Distributors log in with phone + password, not email/username
    (see SPEC.md Section 2.2) — this backend looks the user up by the
    phone number on their Distributor profile instead."""

    def authenticate(self, request, phone_number=None, password=None, **kwargs):
        if not phone_number or not password:
            return None
        try:
            distributor = Distributor.objects.select_related("user").get(
                phone_number=phone_number
            )
        except Distributor.DoesNotExist:
            return None
        user = distributor.user
        if not user.check_password(password):
            return None
        # Task 19 follow-up (CodeRabbit finding, user-confirmed fix):
        # cancel_membership_and_refund credits a real refund to the
        # distributor's own wallet, then sets user.is_active = False --
        # Django's default self.user_can_authenticate(user) checks
        # is_active unconditionally, which would make that credited
        # refund permanently unclaimable (no way to log back in and
        # request a withdrawal). A cooling-off-cancelled distributor is
        # therefore allowed to authenticate despite is_active being
        # False; apps.distributors.views's access-carve-out decorator is
        # what actually restricts what they can do once logged in
        # (withdrawal-related views only). A distributor deactivated for
        # any OTHER reason (e.g. an admin's unrelated suspend toggle,
        # cooling_off_cancelled_at still None) still cannot authenticate
        # at all -- this carve-out is scoped narrowly to that one field.
        if distributor.cooling_off_cancelled_at is not None:
            return user
        if self.user_can_authenticate(user):
            return user
        return None

    def get_user(self, user_id):
        """Overridden for the same reason as authenticate() above:
        ModelBackend.get_user() (inherited otherwise) also blanket-checks
        is_active, and AuthenticationMiddleware calls THIS on every
        request after login to resolve request.user from the session --
        not just once at login time. Without this override, a cooling-off
        -cancelled distributor's authenticate() would succeed but every
        subsequent request would silently resolve to AnonymousUser,
        making the login carve-out above a no-op in practice."""
        user = super().get_user(user_id)
        if user is not None:
            return user
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None
        try:
            distributor = user.distributor
        except Distributor.DoesNotExist:
            return None
        if distributor.cooling_off_cancelled_at is not None:
            return user
        return None
