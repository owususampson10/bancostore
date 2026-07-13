import uuid

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import models

from phonenumber_field.modelfields import PhoneNumberField

from bancostore.media import resize_and_convert_to_webp

KYC_IMAGE_FIELDS = ("ghana_card_front", "ghana_card_back", "selfie")


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

    # Copied over from PendingRegistration (Task 10a/10b) when the
    # registration fee is confirmed paid. Plain fields here, not split into
    # User.first_name/last_name -- those aren't used anywhere else in this
    # project.
    full_name = models.CharField(max_length=255, blank=True, default="")
    address = models.CharField(max_length=255, blank=True, default="")
    area = models.CharField(max_length=255, blank=True, default="")
    landmark = models.CharField(max_length=255, blank=True, default="")

    # Distributors log in with phone + password, not email (see SPEC.md Section 2.2) —
    # unique so it doubles as the login lookup key.
    phone_number = PhoneNumberField(unique=True)
    phone_verified = models.BooleanField(default=False)

    # Wrong-password lockout (Task 5) — thresholds come from django-constance
    # (MAX_FAILED_LOGIN_ATTEMPTS, ACCOUNT_LOCKOUT_DURATION_MINUTES), never hardcoded.
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    # Task 10c (Paystack starter pack). Price/PV/rank are snapshotted from
    # django-constance at selection time (never re-derived live at
    # confirmation time) -- same reasoning as PendingRegistration's
    # fee_amount_pesewas: an admin changing STARTER_PACK_*_PRICE between
    # selection and webhook must not make a genuinely, correctly paid
    # purchase fail an exact-amount check against a since-changed value.
    # `rank` (above) is only set from starter_pack_rank once payment is
    # confirmed; starter_pack_confirmed_at is the definitive idempotency
    # signal, kept separate from `rank` since `rank` could conceivably be
    # set through some other path later (e.g. admin override).
    starter_pack_choice = models.CharField(max_length=1, blank=True, default="")
    starter_pack_price_pesewas = models.PositiveIntegerField(null=True, blank=True)
    starter_pack_pv = models.PositiveIntegerField(null=True, blank=True)
    starter_pack_rank = models.CharField(max_length=50, blank=True, default="")
    starter_pack_payment_reference = models.CharField(
        max_length=100, null=True, blank=True, unique=True
    )
    starter_pack_confirmed_at = models.DateTimeField(null=True, blank=True)

    # Task 11a (KYC submission). Fields live directly on Distributor rather
    # than a separate history-tracking model -- there's exactly one KYC
    # submission per distributor at a time, and resubmission (after a
    # rejection, or before first review) simply overwrites these, matching
    # the same simplicity-first convention as the starter_pack_* fields
    # above. Converted to WebP on save like every other user-uploaded image
    # in this project (Task 7 precedent) via bancostore/media.py.
    ghana_card_front = models.ImageField(upload_to="kyc/ghana_card_front/", blank=True)
    ghana_card_back = models.ImageField(upload_to="kyc/ghana_card_back/", blank=True)
    selfie = models.ImageField(upload_to="kyc/selfie/", blank=True)
    kyc_submitted_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Distributor<{self.user}>"

    def save(self, *args, **kwargs):
        # Same freshly-uploaded-vs-already-saved guard as ProductImage/
        # Category in apps/catalog/models.py -- self.<field>.file is an
        # UploadedFile only for a freshly-assigned upload, avoiding
        # reprocessing (and double-compressing) on every unrelated field
        # save (e.g. a login-attempt counter update).
        for field_name in KYC_IMAGE_FIELDS:
            field = getattr(self, field_name)
            if field and isinstance(field.file, UploadedFile):
                setattr(self, field_name, resize_and_convert_to_webp(field))
        super().save(*args, **kwargs)


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

    # Task 10b (Paystack). Both confirmed via doubt-driven-development
    # 2026-07-13 -- see project_paystack_registration_payment_design memory:
    # fee_amount_pesewas snapshots REGISTRATION_FEE at Paystack-initialize
    # time so a later admin change to the fee can't make a genuinely,
    # correctly paid registration fail an exact-amount check against a
    # since-changed value. payment_reference is regenerated on every visit
    # to the payment step (not the stable `token` above) so a retried
    # payment (declined card, abandoned checkout) gets a fresh Paystack
    # reference rather than reusing one across multiple initialize calls.
    fee_amount_pesewas = models.PositiveIntegerField(null=True, blank=True)
    payment_reference = models.CharField(
        max_length=100, null=True, blank=True, unique=True
    )

    def __str__(self):
        return f"PendingRegistration<{self.phone_number}>"
