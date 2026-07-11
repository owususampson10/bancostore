from django.conf import settings
from django.db import models

from phonenumber_field.modelfields import PhoneNumberField


class Distributor(models.Model):
    class KycStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="distributor",
    )
    sponsor = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="referrals",
    )
    rank = models.CharField(max_length=50, blank=True, default="")
    kyc_status = models.CharField(
        max_length=20, choices=KycStatus.choices, default=KycStatus.PENDING
    )
    ir_id = models.CharField(max_length=50, unique=True, null=True, blank=True)

    # Distributors log in with phone + password, not email (see SPEC.md Section 2.2) —
    # unique so it doubles as the login lookup key.
    phone_number = PhoneNumberField(unique=True)
    phone_verified = models.BooleanField(default=False)

    # Wrong-password lockout (Task 5) — thresholds come from django-constance
    # (MAX_FAILED_LOGIN_ATTEMPTS, ACCOUNT_LOCKOUT_DURATION_MINUTES), never hardcoded.
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Distributor<{self.user}>"
