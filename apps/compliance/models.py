from decimal import Decimal

from django.db import models


class EscrowLedger(models.Model):
    """Task 47a. A SINGLE platform-wide row (pk=1), not one row per
    distributor/order the way Wallet is. Pre-seeded by a data migration
    (0002_seed_escrow_ledger.py, mirroring
    apps.distributors.models.IrIdSequence's own seeding precedent) so
    the row already exists in every real environment before any traffic
    arrives -- apps.compliance.services.credit_escrow/reverse_escrow
    additionally call get_or_create(pk=1) defensively (mirroring
    apps.wallet.services.credit()'s own lazy-creation shape) rather than
    a hard .get(), after real pytest-django test-isolation runs showed
    the row can be legitimately absent under some database-flush
    strategies (transaction=True tests) despite being seeded -- a
    test-infrastructure reality, not a production one.

    balance is a real stored running total: apps.compliance.services
    .credit_escrow/reverse_escrow update it via the same F()-relative
    atomic UPDATE apps.wallet.services.credit/debit use, but NOWAIT-
    locked first (bancostore.concurrency
    .select_for_update_nowait_if_supported) -- a doubt-driven-development
    finding: unlike Wallet's naturally-sharded per-distributor rows,
    this single row sees contention from every confirmed order AND every
    cancelled/refunded order platform-wide, so a plain non-NOWAIT update
    risks blocking for up to MySQL's full lock-wait-timeout instead of
    failing fast into this codebase's existing retry_on_lock_contention
    budget.

    EscrowTransaction is the append-only audit log; balance always
    equals SUM(EscrowTransaction.amount) for this ledger, mirroring
    Wallet.balance's own invariant exactly (credits positive, reversals
    negative -- see EscrowTransaction's own docstring)."""

    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Defense-in-depth, not the primary guard -- the primary
            # guard is credit_escrow/reverse_escrow's own atomic
            # conditional UPDATE, which can never violate this by
            # construction. Mirrors Wallet.balance's own identical
            # reasoning (bulk F()-relative UPDATEs bypass Django's
            # model-level validation entirely; this constraint is the
            # actual guardrail against a future code path touching
            # balance without going through that discipline).
            models.CheckConstraint(
                check=models.Q(balance__gte=0), name="escrow_ledger_balance_gte_0"
            ),
        ]

    def __str__(self):
        return f"EscrowLedger<balance={self.balance}>"


class EscrowTransaction(models.Model):
    """Task 47a. One immutable row per escrow credit or reversal,
    mirroring apps.wallet.models.WalletTransaction's shape exactly: a
    signed amount (CREDIT rows always positive, REVERSAL rows always
    negative) so EscrowLedger.balance == SUM(EscrowTransaction.amount)
    always holds.

    order_id is a plain int, NOT a ForeignKey to Order -- mirroring
    OrderCycleFailure/CommissionCycleFailure/WithdrawalCycleFailure's own
    established reasoning (see OrderCycleFailure's own docstring in
    apps/orders/models.py): this audit record must survive even if the
    Order row it references is later deleted outside the (locked-down)
    admin UI, which is a real, already-documented practice in this
    project (CLAUDE.md's Task 24 production smoke-test cleanup).

    rate_applied snapshots the exact ESCROW_RESERVE_RATE percentage used
    for this row -- that constance setting is admin-editable and
    constance keeps no value history, so without this snapshot a
    compliance audit of an old transaction has no direct way to verify
    `amount` against a since-changed live rate. A REVERSAL row's
    rate_applied is copied from its originating CREDIT row (the rate
    that was actually applied when the money was set aside), never
    recomputed from the current live rate -- matching this codebase's
    established snapshot-at-event-time convention (Task 19's sponsor-
    bonus reversal uses the amount actually credited, not a live
    recomputation, for the identical reason)."""

    class TransactionType(models.TextChoices):
        CREDIT = "credit", "Credit"
        REVERSAL = "reversal", "Reversal"

    ledger = models.ForeignKey(
        EscrowLedger, on_delete=models.PROTECT, related_name="transactions"
    )
    order_id = models.PositiveIntegerField()
    transaction_type = models.CharField(max_length=20, choices=TransactionType.choices)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    rate_applied = models.DecimalField(max_digits=5, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # A CREDIT row is always positive, a REVERSAL row is always
            # negative -- a doubt-driven-development finding: dropping
            # the simpler amount__gt=0 constraint (no longer valid once
            # reversals exist) must not remove ALL DB-level defense
            # against a future sign-flip bug silently corrupting the
            # ledger.
            models.CheckConstraint(
                check=(
                    models.Q(transaction_type="credit", amount__gt=0)
                    | models.Q(transaction_type="reversal", amount__lt=0)
                ),
                name="escrow_transaction_sign_matches_type",
            ),
            # At most one CREDIT and at most one REVERSAL per order -- a
            # structural backstop independent of caller discipline, not
            # just the application-level idempotency check in
            # reverse_escrow. Deliberately NOT a partial/conditional
            # UniqueConstraint (fields=["order_id"],
            # condition=Q(transaction_type="reversal")): this codebase
            # already found and fixed the exact same mistake once on
            # WalletTransaction -- MySQL silently SKIPS creating a
            # conditional unique index entirely rather than erroring, so
            # it would look enforced on SQLite (local/CI... no, local
            # dev) while doing nothing on real MySQL. An unconditional
            # compound constraint on (order_id, transaction_type) gives
            # the identical guarantee (each transaction_type value is
            # independently unique per order) without that gap.
            models.UniqueConstraint(
                fields=["order_id", "transaction_type"],
                name="unique_escrow_transaction_order_type",
            ),
        ]
        indexes = [
            models.Index(fields=["order_id"]),
        ]

    def __str__(self):
        return (
            f"EscrowTransaction<order_id={self.order_id} "
            f"{self.transaction_type} {self.amount}>"
        )
