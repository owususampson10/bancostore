import uuid

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


class PendingRegistration(models.Model):
    """Holds a validated registration submission (Task 10a) until the GHS
    100 registration fee is confirmed paid (Task 10b) -- the real User and
    Distributor aren't created until then. Design confirmed via
    doubt-driven-development 2026-07-13: a DB table (not a Redis cache
    entry) because cache entries are evictable under memory pressure, a
    real durability risk for this data.

    Deliberately NOT tracked by django-simple-history (installed
    project-wide) -- the cleanup task's deletion must actually remove this
    PII, not leave it sitting in a historical table forever.
    """

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    full_name = models.CharField(max_length=255)
    phone_number = PhoneNumberField(unique=True)
    email = models.EmailField()
    address = models.CharField(max_length=255)
    area = models.CharField(max_length=255)
    landmark = models.CharField(max_length=255, blank=True)
    # Pre-hashed via django.contrib.auth.hashers.make_password -- never the
    # plaintext password.
    password_hash = models.CharField(max_length=255)
    sponsor = models.ForeignKey(
        Distributor,
        on_delete=models.CASCADE,
        related_name="pending_registrations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    consumed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"PendingRegistration<{self.phone_number}>"
