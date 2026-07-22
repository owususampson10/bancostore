import uuid

from django.conf import settings
from django.db import models

from phonenumber_field.modelfields import PhoneNumberField
from simple_history.models import HistoricalRecords


class Distributor(models.Model):
    class KycStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class MobileMoneyNetwork(models.TextChoices):
        MTN = "mtn", "MTN MoMo"
        TELECEL = "telecel", "Telecel Cash"
        AIRTELTIGO = "airteltigo", "AirtelTigo Money"

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
    # db_index: apps.commissions.services.distributor_ids_eligible_for_
    # matching_bonus filters on this every Matching Bonus cycle (Task 14,
    # 2026-07-22 code-review finding) -- unindexed at this platform's
    # stated "hundreds of thousands of users" scale would be a full-table
    # scan on every cycle.
    rank = models.CharField(max_length=50, blank=True, default="", db_index=True)
    kyc_status = models.CharField(
        max_length=20, choices=KycStatus.choices, default=KycStatus.PENDING
    )
    ir_id = models.CharField(max_length=50, unique=True, null=True, blank=True)
    # Task 11c: set only on rejection, from KYC_REJECTION_REASONS (constance)
    # or a free-text override entered by the admin.
    kyc_rejection_reason = models.CharField(max_length=255, blank=True, default="")

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

    # Task 16a (ADR-0004): where Paystack Transfer sends a withdrawal
    # payout. Flat fields, not a separate model -- these are a core,
    # directly-owned identity attribute like phone_number, not an
    # externally-sourced structured response like DiditVerification.
    # blank=True/default="" on both (never null=True) to match every other
    # optional field on this model. Deliberately no per-distributor
    # uniqueness constraint -- shared household mobile money wallets are a
    # real, legitimate pattern in Ghana; sponsor/downline collusion via a
    # shared destination is an admin-monitoring concern, not a schema-level
    # block that would also reject genuine distributors. Deliberately no
    # verification flag (unlike phone_number/phone_verified) -- building a
    # second OTP-adjacent flow now would be new scope; Paystack's Transfer
    # Recipient creation (Task 16e) is where an invalid number first
    # actually gets caught.
    mobile_money_number = PhoneNumberField(blank=True, default="")
    mobile_money_network = models.CharField(
        max_length=20, choices=MobileMoneyNetwork.choices, blank=True, default=""
    )

    # Task 11 (observability-and-instrumentation retrospective, 2026-07-14):
    # audit trail for kyc_status/ir_id changes -- who approved/rejected a
    # distributor's KYC, and when. CLAUDE.md already called for this ("log
    # admin actions affecting money or KYC via django-simple-history"), but
    # no model anywhere in the codebase had HistoricalRecords() attached
    # until this retrospective. Captures the acting user automatically via
    # HistoryRequestMiddleware (bancostore/settings.py), which covers both
    # Django Admin actions and any other future save() path. Also now
    # covers mobile_money_number/mobile_money_network (Task 16a) --
    # deliberately useful here too: a payout-destination change shortly
    # before a withdrawal is a real fraud signal worth having in the trail.
    history = HistoricalRecords()

    class Meta:
        constraints = [
            # Task 16a (ADR-0004): a partial save (Django Admin edit, a
            # future form bug) must not silently leave a payout number set
            # with no network, or vice versa. Mirrors Wallet.balance__gte=0
            # (Task 15) -- the same "cheap invariant, real defense in
            # depth" reasoning, not speculative.
            models.CheckConstraint(
                check=(
                    models.Q(mobile_money_number="", mobile_money_network="")
                    | (
                        ~models.Q(mobile_money_number="")
                        & ~models.Q(mobile_money_network="")
                    )
                ),
                name="distributor_payout_destination_both_or_neither",
            ),
        ]

    def __str__(self):
        return f"Distributor<{self.user}>"

    @property
    def has_payout_destination(self):
        """Task 16c will gate withdrawal submission on this, the same way
        kyc_status already gates it -- exposed here so that check has a
        single source of truth rather than each caller re-deriving it from
        the two raw fields."""
        return bool(self.mobile_money_number and self.mobile_money_network)


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


class DiditVerification(models.Model):
    """Task 11a: Didit's hosted KYC verification result (ID front/back +
    selfie face-match/liveness) for one distributor. Kept separate from
    `Distributor` rather than adding a dozen columns there -- this is
    Didit's structured response data, not something Distributor itself
    needs to know about beyond "has one."

    Deliberately informational only: `status` here reflects Didit's own
    decision (including its "in_review" state -- Didit's own way of saying
    "a human should look at this"), but it never sets `Distributor.
    kyc_status` by itself. SPEC.md's Boundaries section is explicit: "Never:
    Auto-approve ... KYC ... even temporarily for admin." Only an admin's
    explicit approve/reject action (Task 11c) can change `kyc_status`.

    Images are fetched from Didit's decision-response media URLs and
    WebP-converted via bancostore/media.py -- a one-time server-side fetch,
    not a Django-form upload, so there's no "freshly uploaded" isinstance
    guard here like Category/ProductImage/the old Task 11a fields had.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        DECLINED = "declined", "Declined"
        IN_REVIEW = "in_review", "In Review"

    distributor = models.OneToOneField(
        Distributor,
        on_delete=models.CASCADE,
        related_name="didit_verification",
    )
    session_id = models.CharField(max_length=100, unique=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )

    # Didit's own per-check status strings (e.g. "Approved"/"Declined"),
    # stored as-is rather than mapped onto our own Status choices -- Didit
    # controls this vocabulary, and normalizing it here risks silently
    # dropping a status value they add later that we haven't accounted for.
    id_verification_status = models.CharField(max_length=20, blank=True, default="")
    face_match_status = models.CharField(max_length=20, blank=True, default="")
    face_match_score = models.FloatField(null=True, blank=True)
    liveness_status = models.CharField(max_length=20, blank=True, default="")
    liveness_score = models.FloatField(null=True, blank=True)

    extracted_full_name = models.CharField(max_length=255, blank=True, default="")
    extracted_document_number = models.CharField(max_length=100, blank=True, default="")
    extracted_date_of_birth = models.DateField(null=True, blank=True)
    warnings = models.JSONField(default=list, blank=True)

    id_front_image = models.ImageField(upload_to="kyc/didit/id_front/", blank=True)
    id_back_image = models.ImageField(upload_to="kyc/didit/id_back/", blank=True)
    selfie_image = models.ImageField(upload_to="kyc/didit/selfie/", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"DiditVerification<{self.distributor} {self.status}>"


class IrIdSequence(models.Model):
    """Task 11c: single-row counter for IR ID generation, so concurrent
    admin approvals never produce a duplicated or reused number. The one
    row (pk=1) is pre-created by a data migration, seeded from
    django-constance's IR_ID_STARTING_NUMBER at migration-run time --
    deliberately NOT lazily created on first use (get_or_create's
    create-then-fall-back-on-IntegrityError path is real but avoidable
    complexity here; pre-seeding sidesteps the cold-start race entirely
    rather than reasoning hard about whether the retry wrapper handles
    every shape of it). IR_ID_STARTING_NUMBER is read exactly this once,
    ever -- changing it later has no effect, since every subsequent IR ID
    comes from incrementing this row, not from re-reading the setting."""

    next_number = models.PositiveIntegerField()

    def __str__(self):
        return f"IrIdSequence<next={self.next_number}>"
