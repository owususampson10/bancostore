import logging
import uuid
from decimal import Decimal

from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from constance import config

from apps.accounts.permissions import is_distributor
from apps.binary_tree.models import BinaryTreeEdge
from apps.catalog.services import InsufficientStockError, decrement_stock
from apps.distributors.paystack import PaystackError, verify_transaction
from apps.notifications.sms import send_sms
from apps.pv_ledger.services import record_personal_pv, record_purchase_pv
from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

from .models import Order, OrderItem

logger = logging.getLogger(__name__)

# Task 18a (ADR-0006 decision 1, read directly against the primary source
# doc's Section 5.2 table). Cancelled is reachable only pre-dispatch
# (pending/confirmed/processing) per the doc's own "Order was cancelled
# before dispatch" wording -- the one restriction Refunded deliberately
# doesn't share, since the doc places no timing restriction on it (a
# delivered order can still be refunded, e.g. a return). Pending/Confirmed
# are the two transitions Task 17c/17d already perform automatically;
# they're listed here too so this graph stays the single source of truth
# for every legal transition, not just the ones Task 18 adds. Cancelled
# and Refunded are terminal -- no transition exists out of either.
_ALLOWED_TRANSITIONS = {
    Order.Status.PENDING: {Order.Status.CONFIRMED, Order.Status.CANCELLED},
    Order.Status.CONFIRMED: {
        Order.Status.PROCESSING,
        Order.Status.CANCELLED,
        Order.Status.REFUNDED,
    },
    Order.Status.PROCESSING: {
        Order.Status.DISPATCHED,
        Order.Status.CANCELLED,
        Order.Status.REFUNDED,
    },
    Order.Status.DISPATCHED: {Order.Status.DELIVERED, Order.Status.REFUNDED},
    Order.Status.DELIVERED: {Order.Status.REFUNDED},
    Order.Status.CANCELLED: set(),
    Order.Status.REFUNDED: set(),
}


def is_legal_order_status_transition(from_status: str, to_status: str) -> bool:
    """Task 18a. Every later slice's transition function (18b's
    cancel/refund reversal, 18c's manual admin transitions, 18d's
    auto-cancel) must call this before writing a new status, so an
    illegal jump fails loudly rather than silently corrupting the
    lifecycle. `_ALLOWED_TRANSITIONS` is the single source of truth --
    see its own comment for the reasoning behind each edge."""
    return to_status in _ALLOWED_TRANSITIONS.get(from_status, set())


def calculate_delivery_fee(delivery_method, delivery_zone, subtotal):
    """Task 17c (ADR-0005 decision 1). Pickup is always free; home
    delivery is free above FREE_DELIVERY_THRESHOLD regardless of zone,
    otherwise the live zone-specific constance rate. Reads config live --
    callers snapshot the returned value onto Order.delivery_fee
    themselves, this function never does the snapshotting."""
    if delivery_method == Order.DeliveryMethod.PICKUP:
        return Decimal("0")
    if subtotal >= config.FREE_DELIVERY_THRESHOLD:
        return Decimal("0")

    zone_fees = {
        Order.DeliveryZone.KUMASI: config.DELIVERY_FEE_KUMASI,
        Order.DeliveryZone.ACCRA: config.DELIVERY_FEE_ACCRA,
        Order.DeliveryZone.OTHER_REGIONS: config.DELIVERY_FEE_OTHER_REGIONS,
    }
    try:
        return zone_fees[delivery_zone]
    except KeyError:
        # doubt-driven-development (Task 17c design review): CheckoutForm
        # already bounds delivery_zone to real choices, so this should be
        # unreachable through the real UI -- raising loudly here (instead
        # of a silent Decimal("0") or letting a bare KeyError surface) is
        # the same "fail loud on an impossible value" discipline as
        # apps/withdrawal/services.py's WITHDRAWAL_FREQUENCY_DURATIONS
        # lookup.
        raise ValueError(f"Unknown delivery_zone: {delivery_zone!r}") from None


def prefill_contact_info(user):
    """Task 17c. A logged-in distributor or customer shouldn't have to
    retype contact info the account already has. Distributor and
    CustomerProfile are two structurally different models (neither has
    its own email field -- only auth.User does), and an anonymous or
    staff-only user has neither, so this must branch explicitly rather
    than assume one shape."""
    if not user.is_authenticated:
        return {}
    if is_distributor(user):
        distributor = user.distributor
        return {
            "full_name": distributor.full_name,
            "phone_number": distributor.phone_number,
            "email": user.email,
        }
    if hasattr(user, "customer_profile"):
        profile = user.customer_profile
        return {
            "full_name": profile.full_name,
            "phone_number": profile.phone_number,
            "email": user.email,
        }
    return {}


def create_pending_order(*, user, cart_items, form_data) -> Order:
    """Task 17c. The sole place an Order/OrderItem row is ever created.
    Snapshots everything (per apps/orders/models.py's own documented
    intent) -- a later change to Product.price/pv_value or the live
    DELIVERY_FEE_*/FREE_DELIVERY_THRESHOLD settings must never
    retroactively change this order.

    doubt-driven-development (design review before implementation) found
    and fixed three real gaps in the original draft: (1) subtotal must be
    computed from the SAME cart_items list every OrderItem snapshot comes
    from, not a second independent call to Cart.subtotal() (which
    re-reads Product.price live) -- an unsynchronized second read could
    make Order.subtotal not equal sum(OrderItem.unit_price * quantity),
    the exact identity tasks/todo.md flags as this function's own
    responsibility to guarantee, since no DB constraint can. (2) the
    Order + all its OrderItems must be one atomic transaction, matching
    every comparable money-adjacent write in this codebase
    (consume_paid_registration, consume_paid_starter_pack) -- a partial
    write on failure would permanently break that same identity.
    (3) order creation logic belongs in the service layer, not inline in
    a view, matching this codebase's universal view-thin/service-thick
    convention.

    Does not decrement stock or credit PV (Task 17d's job once payment is
    confirmed) and does not touch the cart (Task 17d clears it on
    confirmed payment, not here -- an abandoned checkout must not lose
    the customer's cart). No duplicate-submission guard: a double-click
    or back-button resubmit can create more than one pending Order for
    the same cart, each independently valid and payable -- accepted as a
    data-hygiene issue (Task 18's auto-cancel-unpaid-orders job cleans up
    stale ones), not a money-safety one, since nothing is charged until
    Task 17d's Paystack call actually happens against one specific
    payment_reference. Flagged here, not silently unaddressed."""
    if not cart_items:
        # code-review-and-quality (2026-07-25): checkout_view already
        # guards this today, but this function is the one place an Order
        # is ever created -- a defensive check here (matching this
        # codebase's established belt-and-braces convention, e.g.
        # WalletAdmin's own lockdown) means a future caller can't
        # silently create a zero-item, zero-total Order just because it
        # forgot to check first.
        raise ValueError("cart_items must not be empty")

    subtotal = sum((line.line_total for line in cart_items), start=Decimal("0"))
    delivery_fee = calculate_delivery_fee(
        form_data["delivery_method"], form_data.get("delivery_zone", ""), subtotal
    )
    total = subtotal + delivery_fee

    with transaction.atomic():
        order = Order.objects.create(
            customer=user if user.is_authenticated else None,
            full_name=form_data["full_name"],
            phone_number=form_data["phone_number"],
            email=form_data.get("email", ""),
            delivery_method=form_data["delivery_method"],
            delivery_zone=form_data.get("delivery_zone", ""),
            address=form_data.get("address", ""),
            area=form_data.get("area", ""),
            landmark=form_data.get("landmark", ""),
            subtotal=subtotal,
            delivery_fee=delivery_fee,
            total=total,
            # No pre-existing entity id to prefix with (unlike
            # reg-{token}-/pack-{distributor.pk}- elsewhere in this
            # codebase) -- an Order doesn't exist yet at reference-
            # generation time, and each checkout submission is
            # independent, so there's no concurrent-same-entity race for
            # this reference to guard against the way there is for
            # PendingRegistration's reused token.
            payment_reference=f"order-{uuid.uuid4().hex}",
            status=Order.Status.PENDING,
        )
        OrderItem.objects.bulk_create(
            [
                OrderItem(
                    order=order,
                    product=line.product,
                    product_name=line.product.name,
                    quantity=line.quantity,
                    unit_price=line.product.price,
                    unit_pv=line.product.pv_value,
                )
                for line in cart_items
            ]
        )

    return order


def confirm_order_payment(reference: str) -> None:
    """Task 17d (ADR-0005 decision 4). Mirrors
    apps.distributors.services.consume_paid_starter_pack's exact shape:
    lock the Order row, no-op if already resolved, verify_transaction +
    exact pinned-amount check, then inside that same lock decrement stock
    per line item and credit PV, transition status, and send the
    SMS/email confirmation only after the lock releases (never inside
    it) -- matching Task 16g's "never hold a lock across external I/O
    for a notification" standard.

    A fresh-context doubt-driven-development review (two independent
    cross-model passes, reconciled 2026-07-25) before this was written
    found and fixed several gaps against the original draft:

    - Both record_purchase_pv (ancestors) AND record_personal_pv (self)
      must be called for a distributor purchase, matching
      consume_paid_starter_pack's own dual-call pattern exactly -- the
      original draft only had the former, which would have silently
      never built a distributor's own monthly-100-PV eligibility from
      storefront purchases.
    - Order.pv_earned must reflect what was ACTUALLY credited, not the
      pre-computed amount: record_purchase_pv silently no-ops if the
      distributor has no BinaryTreeEdge yet (unplaced -- unreachable
      through normal onboarding, since placement happens at starter-pack
      confirmation, but not database-guaranteed), so placement is checked
      explicitly here before crediting anything.
    - OrderItems are processed in deterministic product_id order before
      decrementing stock, so two orders sharing multiple hot products
      always attempt their per-product locks in the same order --
      reduces (does not need to eliminate, since NOWAIT fails fast
      rather than blocks) contention between concurrent orders.
    - The whole attempt is wrapped in retry_on_lock_contention, matching
      every comparable locked flow in this codebase.
    - verify_transaction's response is read via .get(), never bracket
      indexing -- an unexpected/malformed payload shape must log and
      return, never raise, preserving the "webhook handler never raises"
      contract paystack_webhook depends on.
    - InsufficientStockError is caught here, not left to propagate: per
      ADR-0005 decision 5 ("a later confirmation finding insufficient
      stock must fail the order cleanly... rather than silently
      over-selling"), which the ADR explicitly left the exact terminal
      handling "TBD at implementation time" -- resolved with the user as
      the manual-admin path (below), not automated refund. Real Paystack
      Refund API integration is out of scope here, deliberately deferred
      to Task 18 (which already owns the rest of the Cancelled/Refunded
      lifecycle per this same ADR's Consequences section), not silently
      assumed done.
    - int(order.total * 100) is exact, not a float-precision risk: Order
      is created (17c) from a sum of already-2dp Decimals
      (Product.price * an integer quantity, plus a 2dp delivery fee), so
      order.total is always an exact multiple of one pesewa -- the same
      invariant apps/distributors/services.py already relies on for
      REGISTRATION_FEE/starter_pack_price_pesewas.
    """

    def _attempt():
        with transaction.atomic():
            try:
                order = select_for_update_nowait_if_supported(
                    Order.objects.filter(payment_reference=reference)
                ).get()
            except Order.DoesNotExist:
                logger.error(
                    "confirm_order_payment: no Order found for reference=%s "
                    "-- a payment may have been confirmed with no matching "
                    "record. Needs manual investigation.",
                    reference,
                )
                return

            if order.status != Order.Status.PENDING:
                return  # Already resolved (confirmed or cancelled) -- idempotent no-op.

            try:
                verified = verify_transaction(reference)
            except PaystackError:
                logger.exception(
                    "confirm_order_payment: Paystack verify_transaction "
                    "failed for reference=%s",
                    reference,
                )
                return

            if verified.get("status") != "success":
                return
            if verified.get("currency") != "GHS":
                logger.warning(
                    "confirm_order_payment: unexpected currency %r for " "reference=%s",
                    verified.get("currency"),
                    reference,
                )
                return
            expected_pesewas = int(order.total * 100)
            if verified.get("amount") != expected_pesewas:
                logger.warning(
                    "confirm_order_payment: amount mismatch for "
                    "reference=%s (paid=%r, expected=%r)",
                    reference,
                    verified.get("amount"),
                    expected_pesewas,
                )
                return

            items = list(order.items.select_related("product").order_by("product_id"))
            for item in items:
                decrement_stock(item.product, item.quantity)

            pv_earned = 0
            if order.customer_id is not None and is_distributor(order.customer):
                distributor = order.customer.distributor
                if BinaryTreeEdge.objects.filter(descendant=distributor).exists():
                    pv_amount = sum(item.unit_pv * item.quantity for item in items)
                    if pv_amount:
                        record_purchase_pv(distributor, pv_amount)
                        record_personal_pv(distributor, pv_amount)
                        pv_earned = pv_amount

            order.pv_earned = pv_earned
            order.status = Order.Status.CONFIRMED
            order.confirmed_at = timezone.now()
            order.save(update_fields=["pv_earned", "status", "confirmed_at"])
        _send_confirmation_notifications(order)

    try:
        retry_on_lock_contention(_attempt)
    except InsufficientStockError:
        _cancel_order_for_insufficient_stock(reference)


def _cancel_order_for_insufficient_stock(reference: str) -> None:
    """The prior transaction.atomic() block already rolled back entirely
    (including any earlier line items' stock decrements), so this needs
    its own fresh lock/transaction -- the Order is back to PENDING in the
    database at this point. Manual-admin path, confirmed with the user
    2026-07-25: the customer's payment has already been captured by
    Paystack, so this is CANCELLED (not left PENDING to retry forever on
    every webhook redelivery) with an ERROR-level log for a human to
    action the actual GHS refund -- real Paystack Refund API automation
    is Task 18's scope, not assumed here."""

    def _attempt():
        with transaction.atomic():
            try:
                order = select_for_update_nowait_if_supported(
                    Order.objects.filter(payment_reference=reference)
                ).get()
            except Order.DoesNotExist:
                return
            if order.status != Order.Status.PENDING:
                return  # A concurrent attempt already handled this.
            order.status = Order.Status.CANCELLED
            order.save(update_fields=["status"])
        logger.error(
            "confirm_order_payment: payment verified but stock was "
            "insufficient for reference=%s -- Order CANCELLED, customer "
            "already paid. Needs manual admin refund.",
            reference,
        )
        _send_stock_unavailable_notification(order)

    retry_on_lock_contention(_attempt)


def _send_confirmation_notifications(order: Order) -> None:
    # Each channel is wrapped individually -- money/stock/PV are already
    # committed by this point, so a notification-provider outage must
    # never look like this confirmation was rolled back, and one
    # channel's failure must not skip the other.
    try:
        send_sms(
            str(order.phone_number),
            f"Your Bancostore order (GHS {order.total}) is confirmed! "
            f"Reference: {order.payment_reference}",
        )
    except Exception:
        logger.exception(
            "confirm_order_payment: failed to send SMS confirmation for "
            "reference=%s",
            order.payment_reference,
        )

    if order.email:
        try:
            send_mail(
                subject="Your Bancostore order is confirmed",
                message=(
                    f"Your order (GHS {order.total}) is confirmed. "
                    f"Reference: {order.payment_reference}"
                ),
                from_email=None,
                recipient_list=[order.email],
            )
        except Exception:
            logger.exception(
                "confirm_order_payment: failed to send email confirmation "
                "for reference=%s",
                order.payment_reference,
            )


def _send_stock_unavailable_notification(order: Order) -> None:
    try:
        send_sms(
            str(order.phone_number),
            "We're sorry -- an item in your Bancostore order "
            f"({order.payment_reference}) is no longer available. Your "
            "payment was received; our team will contact you about a "
            "refund.",
        )
    except Exception:
        logger.exception(
            "confirm_order_payment: failed to send stock-unavailable SMS "
            "for reference=%s",
            order.payment_reference,
        )

    if order.email:
        try:
            send_mail(
                subject="An item in your Bancostore order is unavailable",
                message=(
                    "We're sorry -- an item in your order "
                    f"({order.payment_reference}) is no longer available. "
                    "Your payment was received; our team will contact you "
                    "about a refund."
                ),
                from_email=None,
                recipient_list=[order.email],
            )
        except Exception:
            logger.exception(
                "confirm_order_payment: failed to send stock-unavailable "
                "email for reference=%s",
                order.payment_reference,
            )
