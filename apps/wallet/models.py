from decimal import Decimal

from django.db import models


class Wallet(models.Model):
    """One per distributor, created lazily on first credit (see
    services.py::credit) -- mirrors apps/pv_ledger/models.py::PvLedger's
    convention of a OneToOneField created via get_or_create rather than
    eagerly for every distributor."""

    distributor = models.OneToOneField(
        "distributors.Distributor",
        on_delete=models.CASCADE,
        related_name="wallet",
    )
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    class Meta:
        constraints = [
            # Task 15: debit()'s own atomic conditional UPDATE
            # (balance__gte=amount) is the primary guard against this and
            # can never violate it by construction -- this is a separate,
            # structural defense-in-depth layer, mirroring apps.pv_ledger
            # .models.PvDailyBucket's own CheckConstraint(pv__gte=0)
            # reasoning exactly: "bulk F()-relative UPDATEs bypass
            # Django's model-level field validation entirely... this
            # constraint is the actual guardrail" against some future
            # code path touching balance without going through debit()'s
            # own discipline.
            models.CheckConstraint(
                check=models.Q(balance__gte=0), name="wallet_balance_gte_0"
            ),
        ]

    def __str__(self):
        return f"Wallet<{self.distributor_id} balance={self.balance}>"


class WalletTransaction(models.Model):
    """Append-only ledger entry -- balance is a cached aggregate (see
    Wallet.balance), this is the audit trail of every individual credit."""

    class TransactionType(models.TextChoices):
        DIRECT_REFERRAL_BONUS = "direct_referral_bonus", "Direct Referral Bonus"
        BINARY_BONUS = "binary_bonus", "Binary Bonus"
        MATCHING_BONUS = "matching_bonus", "Matching Bonus"
        # Task 15: named directly from this ledger's own spec description
        # ("credit/debit entries... withdrawal debit, refund reversal")
        # even though Tasks 16/19 haven't built the code that produces
        # them yet -- apps.wallet.services.debit() needs a real type to
        # exercise, and these are the two this ledger is documented to
        # need.
        WITHDRAWAL_DEBIT = "withdrawal_debit", "Withdrawal Debit"
        REFUND_REVERSAL = "refund_reversal", "Refund Reversal"
        # Task 16f: credited back when a Paystack Transfer fails/reverses
        # after the wallet was already debited at admin approval (Task
        # 16d). Deliberately distinct from REFUND_REVERSAL -- that type
        # is for product-order refunds, a different domain entirely
        # (doubt-driven-development finding: reusing it would conflate
        # two unrelated reasons money re-enters a wallet).
        WITHDRAWAL_REVERSAL = "withdrawal_reversal", "Withdrawal Reversal"

    wallet = models.ForeignKey(
        Wallet, on_delete=models.CASCADE, related_name="transactions"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=30, choices=TransactionType.choices)
    reference = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Defense-in-depth, not the primary guard (the primary guard is
            # the caller's own idempotency check, e.g.
            # consume_paid_starter_pack's starter_pack_confirmed_at check,
            # or process_binary_bonus_for_distributor's Distributor-row
            # lock). This stops a future call-site bug (a refactor that
            # accidentally calls credit() twice for the same event) from
            # silently double-crediting.
            #
            # Unconditional, not `condition=~models.Q(reference="")` as
            # originally shipped -- MySQL's Django backend has no support
            # for partial/filtered unique indexes and silently SKIPS
            # creating the constraint entirely rather than erroring, so it
            # looked enforced on SQLite (every local/dev test) while doing
            # nothing on real MySQL (production/CI) the whole time this
            # shipped, exactly the same bug class already documented on
            # BinaryTreeEdge's own unique constraint -- confirmed as
            # documented (not just observed) Django behavior: "The
            # condition argument is ignored with MySQL and MariaDB as
            # neither supports conditional indexes."
            # https://docs.djangoproject.com/en/5.0/ref/models/indexes/#condition
            # Caught only once CI's
            # real test suite finally ran again after being silently
            # skipped for days by an unrelated lint failure (see git log).
            # Every current caller of credit() already passes a real,
            # non-blank reference, so this is a behavior-preserving fix,
            # not a functional change -- a future "admin-initiated credit
            # with no natural reference" feature should generate its own
            # synthetic unique reference (e.g. an admin-credit UUID)
            # rather than relying on blank-reference exclusion.
            models.UniqueConstraint(
                fields=["wallet", "reference", "transaction_type"],
                name="unique_wallet_reference_transaction_type",
            )
        ]

    def __str__(self):
        return (
            f"WalletTransaction<{self.wallet_id} {self.amount} {self.transaction_type}>"
        )
