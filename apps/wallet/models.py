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

    def __str__(self):
        return f"Wallet<{self.distributor_id} balance={self.balance}>"


class WalletTransaction(models.Model):
    """Append-only ledger entry -- balance is a cached aggregate (see
    Wallet.balance), this is the audit trail of every individual credit."""

    class TransactionType(models.TextChoices):
        DIRECT_REFERRAL_BONUS = "direct_referral_bonus", "Direct Referral Bonus"
        BINARY_BONUS = "binary_bonus", "Binary Bonus"

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
            # consume_paid_starter_pack's starter_pack_confirmed_at check).
            # This stops a future call-site bug (a refactor that
            # accidentally calls credit() twice for the same event) from
            # silently double-crediting -- a blank reference is common
            # (e.g. admin-initiated credits) so it's excluded rather than
            # treated as one shared "no reference" bucket.
            models.UniqueConstraint(
                fields=["wallet", "reference", "transaction_type"],
                condition=~models.Q(reference=""),
                name="unique_wallet_reference_transaction_type",
            )
        ]

    def __str__(self):
        return (
            f"WalletTransaction<{self.wallet_id} {self.amount} {self.transaction_type}>"
        )
