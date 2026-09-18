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
    # Task 16f (doubt-driven-development finding): create_transfer_recipient
    # requires a name, and ADR-0004's existing snapshot never captured one.
    # Snapshotted alongside the payout destination above, for the same
    # reason -- a distributor editing their profile after approval must
    # not change what 16f later sends to Paystack.
    payout_recipient_name = models.CharField(max_length=255, blank=True, default="")

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

    # Task 68i (agent code review): when the payout was actually handed to
    # Paystack, which is what "stuck waiting for an answer" is measured
    # from. created_at is when the distributor submitted -- a request
    # submitted on Monday and queued by the Friday batch is already days
    # old the moment it is queued, so alerting on created_at would flag
    # nearly every healthy payout.
    queued_for_payout_at = models.DateTimeField(null=True, blank=True)
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
        indexes = [
            # Task 16c (doubt-driven-development, pre-implementation review):
            # submit_withdrawal_request's once-per-WITHDRAWAL_FREQUENCY check
            # filters on exactly these three columns together -- an
            # unindexed composite scan would grow with this distributor's
            # full withdrawal history at this platform's stated
            # "hundreds of thousands of users" scale.
            models.Index(fields=["distributor", "status", "created_at"]),
            # Task 16f PR 2 (code-review-and-quality, 2026-07-24): the
            # withdrawal-payout batch driver's _withdrawal_payout_ids_query
            # filters status__in=[...] ordered by created_at, with no
            # distributor filter -- the composite index above doesn't help
            # here (it only serves queries that also filter on distributor
            # first). Same "hundreds of thousands of users" scale reasoning
            # as the index above, for a genuinely different query shape.
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"WithdrawalRequest<{self.distributor} {self.amount} {self.status}>"


class WithdrawalCycleRun(models.Model):
    """Task 16f PR 2. One durable row per Friday payout batch-driver cycle
    that actually ran -- mirrors apps.commissions.models.CommissionCycleRun's
    shape and rationale (financial jobs with no human review per cycle need
    a durable record, not just ephemeral logging), but is a deliberately
    separate model, not a third job_name value on CommissionCycleRun:
    CommissionCycleFailure.distributor_id can't hold a withdrawal_request_id
    without corrupting the field's meaning, and the two audit trails track
    genuinely different metrics (commission earnings vs. payout outcomes).

    No job_name discriminator -- unlike CommissionCycleRun, exactly one job
    (the Friday payout batch) ever writes here, so there's nothing to
    discriminate between yet. Uniqueness is on run_at alone for the same
    reason."""

    run_at = models.DateTimeField(unique=True)
    evaluated = models.PositiveIntegerField()
    paid = models.PositiveIntegerField()
    failed = models.PositiveIntegerField()
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-run_at"]

    def __str__(self):
        return f"withdrawal payout cycle {self.run_at.isoformat()}"


class WithdrawalCycleFailure(models.Model):
    """One row per WithdrawalRequest whose per-request processing call
    raised during a cycle -- not one row per routine already-resolved
    skip (a row found already paid/rejected/reversed when this cycle
    reached it is not a failure).

    Deliberately not a ForeignKey to WithdrawalRequest, mirroring
    CommissionCycleFailure's own reasoning: a WithdrawalRequest can be
    cascade-deleted if its Distributor is deleted (DistributorAdmin has
    no delete lock, unlike WalletAdmin/WithdrawalRequestAdmin/
    CommissionCycleRunAdmin), and the audit record must survive that.

    Unlike CommissionCycleFailure, also captures distributor_id and
    net_amount at failure time (doubt-driven-development finding,
    2026-07-24): a failed withdrawal payout, unlike a failed commission
    calculation, means real money is already debited and stuck -- a bare
    ID with no amount/distributor leaves no way to reconcile whose money
    it was or how much, if the underlying rows are later deleted. Both
    nullable -- the batch driver captures them from a fresh read of the
    still-live row at failure time, but that read can itself fail (e.g.
    the row vanished between the id-query snapshot and this row's turn in
    the loop), and a missing snapshot must not block recording the
    failure at all."""

    cycle_run = models.ForeignKey(
        WithdrawalCycleRun, on_delete=models.CASCADE, related_name="failures"
    )
    withdrawal_request_id = models.PositiveIntegerField()
    distributor_id = models.PositiveIntegerField(null=True)
    net_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    error = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["withdrawal_request_id"]
        constraints = [
            # code-review-and-quality (2026-07-24): the sole current
            # writer (apps.withdrawal.tasks) always sets distributor_id
            # and net_amount together, or leaves both null -- but per
            # this codebase's own established reasoning (WithdrawalRequest
            # .payout_mobile_money_number/network's identical both-or-
            # neither constraint), a future writer bypassing that
            # discipline is exactly what a DB constraint, not just
            # application code, exists to catch.
            models.CheckConstraint(
                check=(
                    models.Q(distributor_id__isnull=True, net_amount__isnull=True)
                    | (
                        models.Q(distributor_id__isnull=False)
                        & models.Q(net_amount__isnull=False)
                    )
                ),
                name="withdrawal_cycle_failure_snapshot_both_or_neither",
            ),
        ]

    def __str__(self):
        return f"withdrawal_request_id={self.withdrawal_request_id}"
