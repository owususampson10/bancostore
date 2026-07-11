from django.contrib.auth.backends import ModelBackend

from .models import Distributor


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
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
