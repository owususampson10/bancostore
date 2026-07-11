from django.conf import settings
from django.db import models

from phonenumber_field.modelfields import PhoneNumberField


class CustomerProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="customer_profile",
    )
    full_name = models.CharField(max_length=150)
    phone_number = PhoneNumberField()

    def __str__(self):
        return f"CustomerProfile<{self.user}>"


class AdminProfile(models.Model):
    """Wrong-password lockout tracking for admin/staff logins (Task 6) — same
    pattern as Distributor's lockout fields (Task 5), using the same
    django-constance thresholds (MAX_FAILED_LOGIN_ATTEMPTS,
    ACCOUNT_LOCKOUT_DURATION_MINUTES) since SPEC.md applies these rules across
    all account types. Created lazily on first failed/successful admin login
    (see apps/accounts/signals.py) rather than at user-creation time, so it
    works for any is_staff user without needing explicit seeding."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="admin_profile",
    )
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"AdminProfile<{self.user}>"
