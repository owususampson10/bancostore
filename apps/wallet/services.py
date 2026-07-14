from decimal import Decimal

from django.db import transaction
from django.db.models import F

from bancostore.concurrency import retry_on_lock_contention

from .models import Wallet, WalletTransaction


def credit(
    distributor, amount: Decimal, *, transaction_type: str, reference: str
) -> None:
    """Credits `distributor`'s wallet by `amount` and records a
    WalletTransaction ledger entry, atomically. Creates the wallet on
    first use (mirrors apps/pv_ledger/services.py::record_purchase_pv's
    lazy-creation convention).

    Unlike PvLedger (created once per distributor, at placement time),
    a wallet can receive genuinely concurrent credits from unrelated
    events -- multiple downline purchases completing around the same
    time all credit the same sponsor's wallet. retry_on_lock_contention
    (bancostore/concurrency.py) survives that, same as
    consume_paid_starter_pack/approve_kyc.
    """
    if not isinstance(amount, Decimal):
        raise TypeError(
            f"credit() amount must be a Decimal, got {type(amount).__name__}"
        )

    def _attempt():
        with transaction.atomic():
            wallet, _ = Wallet.objects.get_or_create(distributor=distributor)
            Wallet.objects.filter(pk=wallet.pk).update(balance=F("balance") + amount)
            WalletTransaction.objects.create(
                wallet=wallet,
                amount=amount,
                transaction_type=transaction_type,
                reference=reference,
            )

    retry_on_lock_contention(_attempt)
