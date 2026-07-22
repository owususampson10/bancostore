from django.conf import settings
from django.db import models

from phonenumber_field.modelfields import PhoneNumberField
from simple_history.models import HistoricalRecords

from apps.distributors.models import Distributor


class WithdrawalRequest(models.Model):
    """Task 16b. See docs/decisions/0004-withdrawal-payout-design.md for
    the full lifecycle design -- this model is schema only, no service
    logic (that's 16c/16d/16f)."""

    class Status(models.TextChoices):
        SUBMITTED = "submitted", "Submitted"
        APPROVED_DEBITED = "approved_debited", "Approved (debited)"
        QUEUED_FOR_PAYOUT = "queued_for_payout", "Queued for payout"
        PAID = "paid", "Paid"
        PAYOUT_FAILED_REVERSED = "payout_failed_reversed", "Payout failed (reversed)"
        REJECTED = "rejected", "Rejected"

    distributor = models.ForeignKey(
        Distributor, on_delete=models.CASCADE, related_name="withdrawal_requests"
    )
    status = models.CharField(
        max_length=30, choices=Status.choices, default=Status.SUBMITTED
    )

    # Locked in at submission (ADR-0004 point 10) -- authoritative through
    # approval/payout regardless of any later change to the live
    # WITHHOLDING_TAX_RATE/MIN_WITHDRAWAL_AMOUNT/MAX_WITHDRAWAL_AMOUNT
    # constance settings.
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2)

    # Snapshotted from Distributor.mobile_money_number/network at approval
    # time (ADR-0004 point 7), not read live off Distributor afterward --
    # so a distributor editing their payout profile after approval can't
    # redirect an already-debited payout. Blank until approved (16d).
    payout_mobile_money_number = PhoneNumberField(blank=True, default="")
    payout_mobile_money_network = models.CharField(
        max_length=20,
        choices=Distributor.MobileMoneyNetwork.choices,
        blank=True,
        default="",
    )

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True, default="")

    # Populated by 16e/16f. paystack_transfer_reference is the pinned
    # idempotency reference (ADR-0004 point 6) -- null=True/unique=True
    # (not blank=True/default="") so multiple unset rows don't collide on
    # an empty-string uniqueness violation, mirroring
    # Distributor.starter_pack_payment_reference's existing convention.
    paystack_recipient_code = models.CharField(max_length=100, blank=True, default="")
    paystack_transfer_reference = models.CharField(
        max_length=100, null=True, blank=True, unique=True
    )

    created_at = models.DateTimeField(auto_now_add=True)

    # Task 16b: audit trail for every status transition (who approved/
    # rejected/reversed, and when), mirroring Distributor.history's own
    # rationale for kyc_status/ir_id -- SPEC.md's "log admin actions
    # affecting money ... via django-simple-history" applies directly here.
    history = HistoricalRecords()

    class Meta:
        constraints = [
            # code-review-and-quality (2026-07-22): nothing writes these
            # fields yet (16c/16d will), but Wallet.balance__gte=0 (Task 15)
            # and PvDailyBucket.pv__gte=0 established this project's own
            # "bulk/future writes can bypass model-level validation, so the
            # constraint is the actual guardrail" reasoning -- the same
            # applies here before any writer exists, not after. A second
            # CodeRabbit pass (same date) caught that the original version
            # only bounded net_amount <= amount, never the actual arithmetic
            # identity -- amount=500/tax=5/net=100 would have passed
            # silently. Equality plus both non-negative fully subsumes the
            # old <= bound, so it's replaced rather than kept alongside.
            models.CheckConstraint(
                check=(
                    models.Q(amount__gte=0)
                    & models.Q(tax_amount__gte=0)
                    & models.Q(net_amount__gte=0)
                    & models.Q(amount=models.F("tax_amount") + models.F("net_amount"))
                ),
                name="withdrawal_request_amounts_sane",
            ),
            # Same review: the snapshotted payout destination (set at
            # approval, 16d) had no both-or-neither guard, unlike
            # Distributor's own identical two fields (Task 16a) -- a
            # partial snapshot here is just as real a bug as a partial
            # profile save.
            models.CheckConstraint(
                check=(
                    models.Q(
                        payout_mobile_money_number="", payout_mobile_money_network=""
                    )
                    | (
                        ~models.Q(payout_mobile_money_number="")
                        & ~models.Q(payout_mobile_money_network="")
                    )
                ),
                name="withdrawal_request_payout_snapshot_both_or_neither",
            ),
        ]

    def __str__(self):
        return f"WithdrawalRequest<{self.distributor} {self.amount} {self.status}>"
