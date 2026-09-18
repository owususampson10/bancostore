"""Task 68f. What to do when money goes back to a customer on Paystack's
side -- a refund, or a chargeback their bank forced.

Found by the Task 68 audit: nothing in this codebase listened for either.
An order stayed CONFIRMED with its stock decremented, its PV credited and
the sponsor's referral bonus paid, while the money had already left the
merchant account. The only way to notice was to read a bank statement.

Never trusts the webhook body: the transaction is re-verified server-side,
the same rule every other payment path here follows. A fully refunded
Paystack transaction reports status "reversed" -- confirmed against the live
API on 2026-09-18 using this platform's own refunded transaction.

Clawback policy (user-accepted 2026-09-18): the DIRECT REFERRAL bonus only,
best effort, and a logged shortfall rather than a debt if the sponsor has
already withdrawn it. Binary and matching bonuses are never clawed back --
ADR-0006's reasoning stands: they are paid from a pooled PV batch that
cannot be attributed to one sale.
"""

import logging
import re

from django.db import transaction
from django.utils import timezone

from apps.wallet.models import WalletTransaction

from .models import Distributor, PaymentIssue, StarterPackCheckout
from .payment_issues import record_payment_issue
from .payment_outcomes import PaymentOutcome
from .paystack import PaystackError, PaystackNotFoundError, verify_transaction

logger = logging.getLogger(__name__)

# What Paystack reports once the money has actually gone back.
REFUNDED_TRANSACTION_STATUS = "reversed"

# Paystack's own reference charset. The webhook is signature-verified, so
# this is defence in depth rather than the only guard -- but the reference
# goes into a URL path (/transaction/verify/<reference>), and a value
# carrying "/" or ".." would aim that request somewhere else entirely. The
# same reasoning as the registration path's own reference regex.
_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9._=-]{1,100}$")


def handle_external_refund(reference: str) -> PaymentOutcome:
    """A refund event for one of our references. Re-verifies, then undoes
    what the payment did and tells the admin. Never raises."""
    if not _SAFE_REFERENCE.match(reference or ""):
        logger.warning(
            "handle_external_refund: refusing a reference of an unexpected "
            "shape rather than putting it into a Paystack URL."
        )
        return PaymentOutcome.UNKNOWN_REFERENCE

    try:
        verified = verify_transaction(reference)
    except PaystackNotFoundError:
        logger.warning(
            "handle_external_refund: Paystack has never seen reference=%s",
            reference,
        )
        return PaymentOutcome.UNKNOWN_REFERENCE
    except PaystackError:
        logger.exception(
            "handle_external_refund: could not verify reference=%s", reference
        )
        return PaymentOutcome.VERIFY_FAILED

    if verified.get("status") != REFUNDED_TRANSACTION_STATUS:
        # A refund that has been started but has not moved the money yet.
        # Paystack sends another event when it completes.
        logger.info(
            "handle_external_refund: reference=%s is %r, not yet reversed",
            reference,
            verified.get("status"),
        )
        return PaymentOutcome.NOT_PAID

    if reference.startswith("order-"):
        return _refund_order(reference, verified)
    if reference.startswith("pack-"):
        return _refund_starter_pack(reference, verified)
    return _record(
        reference,
        PaymentIssue.Kind.REFUND_RECEIVED,
        verified,
        detail="This registration fee was refunded on Paystack. If the "
        "distributor's account should no longer be active, deactivate it.",
    )


def handle_payment_dispute(reference: str, event_type: str) -> PaymentOutcome:
    """A customer has disputed a payment with their bank. Nothing is undone
    automatically: a dispute can be won, and reversing an order the customer
    still has would be worse than waiting. The admin is told so they can
    answer it within Paystack's deadline."""
    if not _SAFE_REFERENCE.match(reference or ""):
        logger.warning("handle_payment_dispute: reference of an unexpected shape")
        return PaymentOutcome.UNKNOWN_REFERENCE
    return _record(
        reference,
        PaymentIssue.Kind.DISPUTE_OPENED,
        None,
        detail=(
            f"A customer has disputed this payment ({event_type}). Respond in "
            "the Paystack dashboard before their deadline: an unanswered "
            "dispute is lost by default. Nothing on Bancostore has been "
            "changed -- the order stands unless the dispute succeeds."
        ),
    )


def _refund_order(reference, verified) -> PaymentOutcome:
    from apps.orders.models import Order
    from apps.orders.services import cancel_or_refund_order

    order = Order.objects.filter(payment_reference=reference).first()
    if order is None:
        return _record(
            reference,
            PaymentIssue.Kind.REFUND_RECEIVED,
            verified,
            detail="A refund was processed for a payment with no matching "
            "order on Bancostore.",
        )
    if order.status == Order.Status.REFUNDED:
        return PaymentOutcome.ALREADY_APPLIED

    try:
        cancel_or_refund_order(
            order.pk,
            Order.Status.REFUNDED,
            tracking_note="Refunded on Paystack",
            # Never assumed: a refund says money moved, not that goods
            # returned. Task 18b made this an explicit choice for exactly
            # this reason.
            restock=False,
        )
    except Exception:
        logger.exception(
            "handle_external_refund: could not mark order refunded for " "reference=%s",
            reference,
        )

    # Read the order back rather than describe what we hoped happened:
    # cancel_or_refund_order is a quiet no-op for an order that was never
    # confirmed, and an admin reading "its PV was reversed" about an order
    # where nothing was reversed is worse than no detail at all
    # (adversarial security review).
    order.refresh_from_db()
    if order.status == Order.Status.REFUNDED:
        detail = (
            "This payment was refunded on Paystack, so the order was marked "
            "refunded and its PV reversed. Stock was NOT put back: whether "
            "the goods came back is something only you know -- adjust the "
            "product's stock by hand if they did."
        )
    else:
        detail = (
            "This payment was refunded on Paystack, but the order is still "
            f"{order.get_status_display().lower()} -- it could not be marked "
            "refunded automatically. Check it on the order screen and put its "
            "PV and stock right by hand."
        )
    return _record(reference, PaymentIssue.Kind.REFUND_RECEIVED, verified, detail)


def _refund_starter_pack(reference, verified) -> PaymentOutcome:
    """Claws back the sponsor's direct referral bonus, per the policy above.
    The distributor's own membership is left alone: cancelling it is a
    decision with consequences for their downline, and belongs to the admin
    or to the cooling-off flow, not to a webhook."""
    from .cooling_off_services import _reverse_direct_referral_bonus

    # Via the checkout this reference belongs to, falling back to the
    # distributor's current reference for a payment made before Task 68b
    # started recording checkouts. Looking only at the current reference
    # missed every re-selected distributor entirely (adversarial security
    # review) -- the same lookup gap the payment path already fixed.
    checkout = StarterPackCheckout.objects.filter(reference=reference).first()
    if checkout is not None:
        distributor = Distributor.objects.filter(pk=checkout.distributor_id).first()
    else:
        distributor = Distributor.objects.filter(
            starter_pack_payment_reference=reference
        ).first()
    if distributor is None or not distributor.sponsor_id:
        return _record(
            reference,
            PaymentIssue.Kind.REFUND_RECEIVED,
            verified,
            detail="A starter-pack payment was refunded on Paystack.",
        )

    # Guarded on this refund having been handled at all, NOT on a reversal
    # transaction existing: when the sponsor's wallet is empty the clawback
    # correctly debits nothing and writes no transaction, and Paystack sends
    # both refund.pending and refund.processed -- so a wallet-row check alone
    # would claw the bonus back a second time if the sponsor earned in
    # between (agent code review).
    # A durable fact of its own, not a wallet row an empty wallet never
    # writes, nor a PaymentIssue kind that a later dispute event overwrites
    # (adversarial security review).
    if checkout is not None and checkout.refund_handled_at is not None:
        return _record(
            reference,
            PaymentIssue.Kind.REFUND_RECEIVED,
            verified,
            "This starter-pack payment was already handled as refunded.",
        )

    already_reversed = (
        WalletTransaction.objects.filter(
            wallet__distributor_id=distributor.sponsor_id,
            transaction_type=(
                WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS_REVERSAL
            ),
            reference=reference,
        ).exists()
        or PaymentIssue.objects.filter(
            reference=reference, kind=PaymentIssue.Kind.REFUND_RECEIVED
        ).exists()
    )
    reversed_any = False
    failed = False
    if not already_reversed:
        try:
            with transaction.atomic():
                # credited_reference is THIS checkout's own reference: since
                # Task 68b the bonus may have been credited against a
                # checkout that is no longer the distributor's current one
                # (adversarial security review -- the lookup used to miss
                # entirely and report success anyway).
                reversed_any = _reverse_direct_referral_bonus(
                    distributor, reference, credited_reference=reference
                )
        except Exception:
            failed = True
            logger.exception(
                "handle_external_refund: could not reverse the referral bonus "
                "for reference=%s",
                reference,
            )

    if failed:
        detail = (
            "This starter-pack payment was refunded on Paystack, but the "
            "sponsor's direct referral bonus could not be taken back "
            "automatically."
        )
    elif reversed_any:
        detail = (
            "This starter-pack payment was refunded on Paystack. The "
            "sponsor's direct referral bonus has been taken back. The "
            "distributor's membership was left active -- cancel it yourself "
            "if it should end."
        )
    else:
        # Never claim a clawback that did not happen. An empty sponsor wallet
        # is the accepted outcome (money already withdrawn is not pursued),
        # but the admin should be told which of the two it was.
        detail = (
            "This starter-pack payment was refunded on Paystack. The "
            "sponsor's direct referral bonus could NOT be taken back -- "
            "their wallet no longer held it. The distributor's membership "
            "was left active."
        )

    if checkout is not None:
        StarterPackCheckout.objects.filter(pk=checkout.pk).update(
            refund_handled_at=timezone.now()
        )
    return _record(reference, PaymentIssue.Kind.REFUND_RECEIVED, verified, detail)


def _record(reference, kind, verified, detail) -> PaymentOutcome:
    issue = record_payment_issue(reference, kind, verified=verified, detail=detail)
    if issue is None:
        return PaymentOutcome.VERIFY_FAILED
    return PaymentOutcome.ISSUE_RECORDED
