import logging
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import F

from constance import config

from bancostore.concurrency import select_for_update_nowait_if_supported

from .models import EscrowLedger, EscrowTransaction

logger = logging.getLogger(__name__)


def credit_escrow(order) -> None:
    """Task 47a. Credits the platform-wide EscrowLedger (pk=1) by
    ESCROW_RESERVE_RATE% of `order`'s NET product revenue (subtotal
    minus discount_amount -- delivery_fee never enters this calculation;
    a discount reduces the money the platform actually collected for
    products, so escrow shouldn't be held against money never received).
    Order.subtotal - Order.discount_amount can never go negative --
    Order's own order_amounts_sane CheckConstraint already enforces
    discount_amount <= subtotal.

    Called unconditionally for EVERY confirmed order (guest, customer,
    AND distributor purchases alike) -- a doubt-driven-development
    finding against an earlier draft that would have placed this call
    inside confirm_order_payment's distributor-only PV-credit block,
    silently escrowing only distributor purchases. "Product revenue" has
    no such restriction.

    A zero rate (ESCROW_RESERVE_RATE == 0, a legitimate admin-configured
    "turn this off" state, exactly like WEEKLY_BINARY_BONUS_CAP=0
    elsewhere in this codebase) is an explicit no-op: no EscrowLedger
    update, no EscrowTransaction row. Without this, a 0% rate would
    compute amount=Decimal("0.00") and violate
    EscrowTransaction.escrow_transaction_sign_matches_type's own
    CheckConstraint for a CREDIT row -- inside confirm_order_payment's
    atomic block, which paystack_webhook and order_payment_callback both
    depend on never raising. A doubt-driven-development review rated
    this Critical.

    Does NOT retry on lock contention itself -- same contract as
    apps.wallet.services.credit/debit: always called from within an
    already-retried caller (confirm_order_payment's own
    retry_on_lock_contention(_attempt)). NOWAIT-locks the EscrowLedger
    row before updating it (unlike Wallet.credit's plain conditional
    UPDATE) -- a doubt-driven-development finding: this is a SINGLE
    platform-wide row every confirmed order contends for, unlike
    Wallet's naturally-sharded per-distributor rows, so a plain UPDATE
    risks blocking for up to MySQL's full lock-wait-timeout instead of
    failing fast into the existing retry budget."""
    rate = config.ESCROW_RESERVE_RATE
    net_product_revenue = order.subtotal - order.discount_amount
    amount = (net_product_revenue * rate / Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    if amount == 0:
        return

    with transaction.atomic():
        # get_or_create is the primary safety net only in the sense that
        # it makes this call robust regardless of DB state -- in every
        # real environment the migration-seeded row (0002_seed_escrow_
        # ledger.py) already exists by the time any traffic arrives,
        # mirroring apps.wallet.services.credit()'s own identical
        # lazy-creation shape for a distributor's Wallet.
        EscrowLedger.objects.get_or_create(pk=1)
        ledger = select_for_update_nowait_if_supported(
            EscrowLedger.objects.filter(pk=1)
        ).get()
        updated = EscrowLedger.objects.filter(pk=ledger.pk).update(
            balance=F("balance") + amount
        )
        if not updated:
            raise EscrowLedger.DoesNotExist(
                f"credit_escrow(): EscrowLedger pk={ledger.pk} disappeared "
                f"mid-credit"
            )
        EscrowTransaction.objects.create(
            ledger=ledger,
            order_id=order.pk,
            transaction_type=EscrowTransaction.TransactionType.CREDIT,
            amount=amount,
            rate_applied=rate,
        )
        # The on-call question this answers: "was this order's escrow
        # actually credited, for how much, at what rate?" -- without
        # this, that's a database query, not a log search.
        logger.info(
            "credit_escrow: order_id=%s amount=%s rate=%s", order.pk, amount, rate
        )


def reverse_escrow(order_id) -> None:
    """Task 47a. Reverses a previously-credited escrow amount when an
    order is cancelled/refunded -- a "rolling reserve" model (matching
    how real payment processors like Stripe/PayPal/Paystack handle
    merchant reserve holdbacks): the reserve was tied to a specific
    order's revenue, so refunding that order releases its share back.
    A user-confirmed design choice, deliberately NOT the accumulate-only
    alternative that was also considered (which would have mirrored
    Binary/Matching Bonus's own "never clawed back" precedent) -- those
    are third-party payouts, not a reserve held against a specific
    order's own revenue, so that precedent doesn't transfer here.

    Reverses the EXACT amount originally credited (looked up from the
    order's own CREDIT EscrowTransaction row via .get(), which fails
    loud with MultipleObjectsReturned rather than silently picking one
    if that one-credit-per-order invariant is ever violated), never
    recomputed from the current live ESCROW_RESERVE_RATE -- matching
    this codebase's established snapshot-at-event-time convention
    (Task 19's sponsor-bonus reversal uses the same reasoning).

    A no-op if no CREDIT row exists for this order (a legacy order from
    before this feature existed -- unreachable through normal use since
    cancel_or_refund_order only operates on already-CONFIRMED+ orders,
    which always credit escrow, per credit_escrow's own unconditional
    placement) or if a REVERSAL row already exists (idempotent no-op;
    defense-in-depth on top of the structural
    unique_escrow_transaction_order_type constraint and on top of
    cancel_or_refund_order's own Order-row lock, which is what actually
    makes a concurrent double-call on the same order_id unreachable in
    practice).

    MUST be called from within a transaction that already holds an
    exclusive lock scoped to this order_id for the duration of the call
    -- currently only apps.orders.services.cancel_or_refund_order
    provides this (via its own select_for_update_nowait_if_supported
    lock on the Order row). Calling this from an unlocked context
    reintroduces a real TOCTOU race on the already-reversed check below.
    Does NOT retry on lock contention itself -- same contract as
    credit_escrow above."""
    try:
        credit_txn = EscrowTransaction.objects.get(
            order_id=order_id,
            transaction_type=EscrowTransaction.TransactionType.CREDIT,
        )
    except EscrowTransaction.DoesNotExist:
        return

    already_reversed = EscrowTransaction.objects.filter(
        order_id=order_id,
        transaction_type=EscrowTransaction.TransactionType.REVERSAL,
    ).exists()
    if already_reversed:
        return

    with transaction.atomic():
        ledger = select_for_update_nowait_if_supported(
            EscrowLedger.objects.filter(pk=credit_txn.ledger_id)
        ).get()
        updated = EscrowLedger.objects.filter(pk=ledger.pk).update(
            balance=F("balance") - credit_txn.amount
        )
        if not updated:
            raise EscrowLedger.DoesNotExist(
                f"reverse_escrow(): EscrowLedger pk={ledger.pk} disappeared "
                f"mid-reversal"
            )
        EscrowTransaction.objects.create(
            ledger=ledger,
            order_id=order_id,
            transaction_type=EscrowTransaction.TransactionType.REVERSAL,
            amount=-credit_txn.amount,
            rate_applied=credit_txn.rate_applied,
        )
        logger.info(
            "reverse_escrow: order_id=%s amount=%s", order_id, -credit_txn.amount
        )
