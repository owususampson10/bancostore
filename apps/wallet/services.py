import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import F

from .models import Wallet, WalletTransaction

logger = logging.getLogger(__name__)


class InsufficientBalanceError(Exception):
    """Raised by debit() when distributor's current wallet balance is
    less than the requested debit amount."""


def credit(
    distributor, amount: Decimal, *, transaction_type: str, reference: str
) -> None:
    """Credits `distributor`'s wallet by `amount` and records a
    WalletTransaction ledger entry, atomically. Creates the wallet on
    first use (mirrors apps/pv_ledger/services.py::record_purchase_pv's
    lazy-creation convention).

    Does NOT retry on lock contention itself -- same as
    record_purchase_pv, which relies entirely on being called from
    within an already-retried caller (e.g. consume_paid_starter_pack's
    own retry_on_lock_contention). Wrapping here too would nest retries:
    if this exhausted its own retry budget, the exception would
    propagate to the caller's retry loop, which would then rerun
    *everything* the caller did (including re-verifying payment with
    Paystack) for another full retry budget -- amplifying load on
    exactly the row that's already contended, instead of just retrying
    the credit itself. If you call credit() from a context with no
    surrounding retry_on_lock_contention, wrap the call yourself:
    `retry_on_lock_contention(lambda: credit(...))`.

    Returns None -- `wallet.balance` read before this call is stale
    after it; re-fetch (`Wallet.objects.get(distributor=...)`) if you
    need the post-credit balance.
    """
    if not isinstance(amount, Decimal):
        raise TypeError(
            f"credit() amount must be a Decimal, got {type(amount).__name__}"
        )
    if amount <= 0:
        raise ValueError(f"credit() amount must be positive, got {amount}")
    if distributor is None:
        raise TypeError("credit() distributor must not be None")
    if transaction_type not in WalletTransaction.TransactionType.values:
        raise ValueError(f"credit() unknown transaction_type: {transaction_type!r}")

    with transaction.atomic():
        wallet, _ = Wallet.objects.get_or_create(distributor=distributor)
        updated = Wallet.objects.filter(pk=wallet.pk).update(
            balance=F("balance") + amount
        )
        if not updated:
            raise Wallet.DoesNotExist(
                f"credit(): wallet pk={wallet.pk} disappeared mid-credit"
            )
        WalletTransaction.objects.create(
            wallet=wallet,
            amount=amount,
            transaction_type=transaction_type,
            reference=reference,
        )
        # The on-call question this answers: "was distributor X's wallet
        # actually credited, for how much, and from which event?" --
        # without this, that's a database query, not a log search.
        logger.info(
            "credit: distributor_id=%s amount=%s transaction_type=%s reference=%s",
            distributor.pk,
            amount,
            transaction_type,
            reference,
        )


def debit(
    distributor, amount: Decimal, *, transaction_type: str, reference: str
) -> None:
    """Symmetric to credit() above -- decrements `distributor`'s wallet by
    `amount` and records a WalletTransaction ledger entry storing
    `-amount` (not `amount`), so `Wallet.balance` always equals
    `SUM(WalletTransaction.amount)` for that wallet (Task 15's "balance
    is always the sum of ledger entries" acceptance criterion, satisfied
    literally rather than by convention). Callers still pass a positive
    `amount`, exactly like credit() -- the sign flip is this function's
    own internal concern.

    Race-safety: the balance check and the decrement happen in ONE atomic
    conditional UPDATE (`.filter(balance__gte=amount).update(...)`), the
    same F()-relative-update-is-race-free reasoning credit() above
    already relies on for its own increment -- not a separate locking
    read + Python check-and-modify (unlike
    apps.catalog.services.decrement_stock's pattern in a different app),
    since this file's own established style is already "just use F()
    atomically." By construction, this can never leave `Wallet.balance`
    negative -- Wallet's own `CheckConstraint(balance__gte=0)` is a
    separate, structural defense-in-depth layer for some future code path
    that bypasses this function's discipline entirely, not the normal
    case here.

    Raises InsufficientBalanceError (not a raw IntegrityError) if the
    conditional update affects zero rows -- covers both a genuinely
    insufficient balance and a wallet that's never been credited (lazily
    created here at balance 0, for which any positive debit is
    insufficient by definition).

    Same no-self-retry contract as credit() -- see that docstring. Wrap
    in `retry_on_lock_contention(lambda: debit(...))` yourself if calling
    from outside an already-retried caller.

    Returns None -- same stale-balance caveat as credit()."""
    if not isinstance(amount, Decimal):
        raise TypeError(
            f"debit() amount must be a Decimal, got {type(amount).__name__}"
        )
    if amount <= 0:
        raise ValueError(f"debit() amount must be positive, got {amount}")
    if distributor is None:
        raise TypeError("debit() distributor must not be None")
    if transaction_type not in WalletTransaction.TransactionType.values:
        raise ValueError(f"debit() unknown transaction_type: {transaction_type!r}")

    with transaction.atomic():
        wallet, _ = Wallet.objects.get_or_create(distributor=distributor)
        updated = Wallet.objects.filter(pk=wallet.pk, balance__gte=amount).update(
            balance=F("balance") - amount
        )
        if not updated:
            raise InsufficientBalanceError(
                f"debit(): distributor_id={distributor.pk} has insufficient "
                f"balance to debit {amount}"
            )
        WalletTransaction.objects.create(
            wallet=wallet,
            amount=-amount,
            transaction_type=transaction_type,
            reference=reference,
        )
        logger.info(
            "debit: distributor_id=%s amount=%s transaction_type=%s reference=%s",
            distributor.pk,
            amount,
            transaction_type,
            reference,
        )
