import logging
import uuid
from decimal import Decimal

from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from constance import config

from apps.accounts.permissions import is_distributor
from apps.binary_tree.models import BinaryTreeEdge
from apps.catalog.services import (
    InsufficientStockError,
    decrement_stock,
    increment_stock,
)
from apps.distributors.models import Distributor
from apps.distributors.paystack import PaystackError, verify_transaction
from apps.notifications.sms import send_sms
from apps.pv_ledger.models import MonthlyPersonalPv, PvDailyBucket, PvLedger
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

            # Captured once, reused for both the PV credit's date/period
            # and confirmed_at below -- record_purchase_pv/record_personal_pv
            # each hardcoded their own independent timezone.now() call
            # until Task 18b, meaning the credit's date and confirmed_at
            # could disagree across a real midnight boundary (real work,
            # including a retry-with-sleep loop, happens between them).
            # A later cancel/refund reversal targets PvDailyBucket/
            # MonthlyPersonalPv via confirmed_at.date() -- it must always
            # be the exact date/period the original credit landed in.
            now = timezone.now()
            pv_earned = 0
            if order.customer_id is not None and is_distributor(order.customer):
                distributor = order.customer.distributor
                if BinaryTreeEdge.objects.filter(descendant=distributor).exists():
                    pv_amount = sum(item.unit_pv * item.quantity for item in items)
                    if pv_amount:
                        record_purchase_pv(distributor, pv_amount, today=now.date())
                        record_personal_pv(distributor, pv_amount, today=now.date())
                        pv_earned = pv_amount

            if not is_legal_order_status_transition(
                order.status, Order.Status.CONFIRMED
            ):
                # Unreachable given the PENDING guard above -- this is a
                # tripwire against _ALLOWED_TRANSITIONS drifting out of
                # sync with this call site (CodeRabbit, PR #28).
                raise RuntimeError(
                    f"Illegal order status transition: {order.status} -> "
                    f"confirmed for reference={reference}"
                )
            order.pv_earned = pv_earned
            order.status = Order.Status.CONFIRMED
            order.confirmed_at = now
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
            if not is_legal_order_status_transition(
                order.status, Order.Status.CANCELLED
            ):
                # Unreachable given the PENDING guard above -- same
                # tripwire as confirm_order_payment's (CodeRabbit, PR #28).
                raise RuntimeError(
                    f"Illegal order status transition: {order.status} -> "
                    f"cancelled for reference={reference}"
                )
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


def _auto_cancel_pending_order(order_id) -> bool:
    """Task 18d. Per-order auto-cancel for
    apps.orders.tasks.auto_cancel_unpaid_orders -- mirrors
    cancel_or_refund_order/advance_order_status's locked shape, but for
    the `pending` -> `cancelled` transition specifically. Per ADR-0006
    decision 6, nothing was ever charged, decremented, or credited for a
    still-pending order (ADR-0005 decision 3/4), so there is nothing to
    reverse -- a pure status transition plus notification.

    Returns True if this call actually cancelled the order, False if it
    was a no-op (already resolved by something else -- e.g. the customer
    completed payment in the same instant this cycle reached it, or a
    previous cycle/manual action already resolved it)."""

    def _attempt():
        with transaction.atomic():
            try:
                locked_order = select_for_update_nowait_if_supported(
                    Order.objects.filter(pk=order_id)
                ).get()
            except Order.DoesNotExist:
                logger.error("_auto_cancel_pending_order: no Order pk=%s", order_id)
                return False

            if locked_order.status != Order.Status.PENDING:
                return False  # Already resolved concurrently -- safe no-op.

            if not is_legal_order_status_transition(
                locked_order.status, Order.Status.CANCELLED
            ):
                # Unreachable given the PENDING check above (pending ->
                # cancelled is always legal per 18a's graph) -- same
                # tripwire convention as confirm_order_payment's own.
                raise RuntimeError(
                    f"Illegal order status transition: "
                    f"{locked_order.status} -> cancelled for order "
                    f"pk={order_id}"
                )

            locked_order.status = Order.Status.CANCELLED
            locked_order.save(update_fields=["status"])
        _send_auto_cancel_notification(locked_order)
        return True

    return retry_on_lock_contention(_attempt)


def _send_auto_cancel_notification(order: Order) -> None:
    # Each channel wrapped individually, matching every other
    # notification helper in this module -- a notification-provider
    # outage must never look like this cancellation was rolled back.
    try:
        send_sms(
            str(order.phone_number),
            f"Your Bancostore order {order.payment_reference} was "
            "automatically cancelled because payment was not completed "
            "in time.",
        )
    except Exception:
        logger.exception(
            "_auto_cancel_pending_order: failed to send SMS for " "reference=%s",
            order.payment_reference,
        )

    if order.email:
        try:
            send_mail(
                subject="Your Bancostore order was cancelled",
                message=(
                    f"Your order {order.payment_reference} was "
                    "automatically cancelled because payment was not "
                    "completed in time."
                ),
                from_email=None,
                recipient_list=[order.email],
            )
        except Exception:
            logger.exception(
                "_auto_cancel_pending_order: failed to send email for " "reference=%s",
                order.payment_reference,
            )


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


ADVANCEABLE_STATUSES = (
    Order.Status.PROCESSING,
    Order.Status.DISPATCHED,
    Order.Status.DELIVERED,
)


def advance_order_status(order_id, to_status, tracking_note="") -> None:
    """Task 18c. Admin-triggered forward progression through the
    non-money-adjacent stages (Processing, Dispatched, Delivered --
    Delivered is admin-only per ADR-0006's stated assumption, no
    separate Delivery role exists). Mirrors `cancel_or_refund_order`'s
    locked, idempotent shape (Task 18b) minus the stock/PV reversal,
    which these stages never need (nothing was reversed at any point --
    the order stays `confirmed` in every money-adjacent sense).

    `to_status` MUST be one of `ADVANCEABLE_STATUSES` -- `Cancelled`/
    `Refunded` belong to `cancel_or_refund_order`, not this function,
    same "reject the wrong target outright" discipline as that
    function's own `to_status` guard."""
    if to_status not in ADVANCEABLE_STATUSES:
        raise ValueError(
            f"advance_order_status: to_status must be one of "
            f"{ADVANCEABLE_STATUSES}, got {to_status!r}."
        )

    def _attempt():
        with transaction.atomic():
            try:
                locked_order = select_for_update_nowait_if_supported(
                    Order.objects.filter(pk=order_id)
                ).get()
            except Order.DoesNotExist:
                logger.error("advance_order_status: no Order pk=%s", order_id)
                return

            if locked_order.status == to_status:
                return  # Idempotent no-op -- a duplicate admin click.

            if not is_legal_order_status_transition(locked_order.status, to_status):
                raise RuntimeError(
                    f"Illegal order status transition: "
                    f"{locked_order.status} -> {to_status} for order "
                    f"pk={order_id}"
                )

            locked_order.status = to_status
            if tracking_note and tracking_note.strip():
                locked_order.tracking_note = tracking_note
            locked_order.save(update_fields=["status", "tracking_note"])
        _send_order_status_notification(locked_order)

    retry_on_lock_contention(_attempt)


def cancel_or_refund_order(order_id, to_status, tracking_note="", restock=None) -> None:
    """Task 18b (ADR-0006, three-cycle doubt-driven-development pass,
    2026-07-26). Cancels or refunds an already-paid order: locks the
    `Order` row, validates the transition (18a), and reverses stock and
    PV (`PvLedger`, `PvDailyBucket`, `MonthlyPersonalPv`) inside that same
    lock before transitioning status and notifying after the lock
    releases -- mirroring `confirm_order_payment`'s locked, idempotent
    shape (Task 17d) in reverse.

    `to_status` MUST be `Order.Status.CANCELLED` or `.REFUNDED` -- every
    other legal transition (Processing/Dispatched/Delivered) belongs to
    Task 18c's own function. This is enforced here, not just documented:
    a round-3 doubt-driven-development finding caught that an earlier
    draft let any `is_legal_order_status_transition`-legal target through,
    meaning a caller mistake (e.g. passing `PROCESSING`) would silently
    reverse a perfectly healthy order's PV/stock.

    `restock` is REQUIRED (no silent default) whenever `to_status` is
    `REFUNDED` -- matches standard e-commerce practice (e.g. Shopify's
    refund flow has an explicit "Restock items" choice, not automatic):
    a refund doesn't always mean the goods physically came back (a
    goodwill credit, or a damaged item the customer keeps). Cancellation
    is pre-dispatch only (per the 18a transition graph) so the goods
    never shipped -- it always restocks automatically, no argument
    needed.

    This function only operates on orders that are ALREADY paid
    (`confirmed`/`processing`/`dispatched`/`delivered`) -- a `pending`
    order is a caller bug (a separate, existing function,
    `_cancel_order_for_insufficient_stock`, plus Task 18d's future
    auto-cancel, own the unpaid `pending` -> `cancelled` path, which has
    nothing to reverse since nothing was ever decremented/credited).

    **Accepted limitation (ADR-0006, wider than its original sunk-cost
    acceptance):** `PvDailyBucket` is a fungible same-day pool shared by
    every order crediting the same ancestor+leg -- a reversal can only
    check whether the pool currently holds enough to reverse, not whether
    what remains is genuinely this order's own contribution versus a
    different, still-valid sibling order's. Real per-order PV attribution
    would need a much larger schema change; not built here."""
    if to_status not in (Order.Status.CANCELLED, Order.Status.REFUNDED):
        raise ValueError(
            f"cancel_or_refund_order: to_status must be CANCELLED or "
            f"REFUNDED, got {to_status!r}."
        )
    if to_status == Order.Status.REFUNDED and restock is None:
        raise ValueError(
            "cancel_or_refund_order: restock (True/False) is required "
            "when to_status is REFUNDED -- whether the goods were "
            "physically returned is never assumed."
        )

    def _attempt():
        with transaction.atomic():
            try:
                locked_order = select_for_update_nowait_if_supported(
                    Order.objects.filter(pk=order_id)
                ).get()
            except Order.DoesNotExist:
                logger.error("cancel_or_refund_order: no Order pk=%s", order_id)
                return

            if locked_order.status == Order.Status.PENDING:
                logger.warning(
                    "cancel_or_refund_order: called on a PENDING order "
                    "pk=%s -- this function is for already-paid orders "
                    "only; caller bug.",
                    order_id,
                )
                return

            if locked_order.status in (
                Order.Status.CANCELLED,
                Order.Status.REFUNDED,
            ):
                if locked_order.status != to_status:
                    logger.warning(
                        "cancel_or_refund_order: order pk=%s already "
                        "resolved as %s, but this call requested %s -- "
                        "ignoring (idempotent no-op).",
                        order_id,
                        locked_order.status,
                        to_status,
                    )
                return  # Idempotent no-op either way.

            if not is_legal_order_status_transition(locked_order.status, to_status):
                raise RuntimeError(
                    f"Illegal order status transition: "
                    f"{locked_order.status} -> {to_status} for order "
                    f"pk={order_id}"
                )

            if locked_order.pv_earned > 0:
                _reverse_ancestor_pv(locked_order, order_id)

            should_restock = to_status == Order.Status.CANCELLED or restock
            if should_restock:
                for item in locked_order.items.select_related("product").order_by(
                    "product_id"
                ):
                    increment_stock(item.product, item.quantity)

            locked_order.pv_earned = 0
            locked_order.status = to_status
            if tracking_note and tracking_note.strip():
                locked_order.tracking_note = tracking_note
            locked_order.save(update_fields=["pv_earned", "status", "tracking_note"])
        _send_order_status_notification(locked_order)

    retry_on_lock_contention(_attempt)


def _reverse_ancestor_pv(locked_order: Order, order_id) -> None:
    """Reverses `locked_order.pv_earned` across `PvLedger`,
    `PvDailyBucket`, and `MonthlyPersonalPv` for the purchasing
    distributor's ancestors (and, for `MonthlyPersonalPv`, the
    purchasing distributor themselves) -- called from inside
    `cancel_or_refund_order`'s own lock, before stock reversal."""
    if locked_order.customer_id is None:
        logger.warning(
            "cancel_or_refund_order: order pk=%s has pv_earned=%s but "
            "customer_id is now NULL (account deleted since "
            "confirmation) -- PV ledger reversal skipped, pv_earned "
            "zeroed anyway.",
            order_id,
            locked_order.pv_earned,
        )
        return

    try:
        distributor = locked_order.customer.distributor
    except Distributor.DoesNotExist:
        logger.warning(
            "cancel_or_refund_order: order pk=%s has pv_earned=%s but "
            "its customer is no longer a distributor -- PV ledger "
            "reversal skipped, pv_earned zeroed anyway.",
            order_id,
            locked_order.pv_earned,
        )
        return

    edges = list(BinaryTreeEdge.objects.filter(descendant=distributor))
    if not edges:
        return

    left_ids = sorted(
        {e.ancestor_id for e in edges if e.leg == BinaryTreeEdge.Leg.LEFT}
    )
    right_ids = sorted(
        {e.ancestor_id for e in edges if e.leg == BinaryTreeEdge.Leg.RIGHT}
    )
    all_ancestor_ids = sorted(set(left_ids) | set(right_ids))

    # Locked one at a time, in sorted-pk order -- deliberately mirrors
    # apps.binary_tree.services.BinaryTree.place_distributor's own
    # "fixed, PK-ascending order" convention (a bulk multi-row NOWAIT
    # statement's internal lock-acquisition order isn't a fact this
    # codebase has verified against real InnoDB docs, so this reuses the
    # pattern it HAS already established and proven instead). Needed so
    # this reversal actually serializes against a concurrent Binary Bonus
    # cycle for any of these ancestors -- apps.commissions.services.
    # process_binary_bonus_for_distributor locks the exact same row
    # (Distributor, not PvDailyBucket) before its own read-then-write
    # PvDailyBucket consumption.
    for pk in all_ancestor_ids:
        select_for_update_nowait_if_supported(Distributor.objects.filter(pk=pk)).get()

    pv_amount = locked_order.pv_earned
    purchase_date = locked_order.confirmed_at.date()

    for field, leg, ancestor_ids in (
        ("left_leg_pv", BinaryTreeEdge.Leg.LEFT, left_ids),
        ("right_leg_pv", BinaryTreeEdge.Leg.RIGHT, right_ids),
    ):
        if not ancestor_ids:
            continue

        # PvLedger: nothing else has ever decremented it (append-only
        # until this function), so a shortfall here is a real bug (a
        # double reversal, or a pv_earned/ledger mismatch elsewhere) --
        # logged at ERROR, unlike the two expected-gap cases below.
        sufficient_ids = set(
            PvLedger.objects.filter(
                distributor_id__in=ancestor_ids, **{f"{field}__gte": pv_amount}
            ).values_list("distributor_id", flat=True)
        )
        PvLedger.objects.filter(distributor_id__in=sufficient_ids).update(
            **{field: F(field) - pv_amount}
        )
        short_ids = set(ancestor_ids) - sufficient_ids
        if short_ids:
            logger.error(
                "cancel_or_refund_order: order pk=%s could not fully "
                "reverse PvLedger.%s (%s leg) for ancestor(s)=%s -- "
                "ledger already below pv_amount=%s. Needs manual "
                "investigation.",
                order_id,
                field,
                leg,
                sorted(short_ids),
                pv_amount,
            )

        # PvDailyBucket: a shortfall here IS an expected, accepted gap
        # (ADR-0006) -- the PV may have already been consumed by a
        # completed Binary Bonus cycle, or expired past
        # PV_CARRY_FORWARD_EXPIRY_DAYS. Logged at WARNING, not raised.
        sufficient_ids = set(
            PvDailyBucket.objects.filter(
                distributor_id__in=ancestor_ids,
                leg=leg,
                date=purchase_date,
                pv__gte=pv_amount,
            ).values_list("distributor_id", flat=True)
        )
        PvDailyBucket.objects.filter(
            distributor_id__in=sufficient_ids, leg=leg, date=purchase_date
        ).update(pv=F("pv") - pv_amount)
        short_ids = set(ancestor_ids) - sufficient_ids
        if short_ids:
            logger.warning(
                "cancel_or_refund_order: order pk=%s only reversed "
                "PvDailyBucket for %s/%s %s-leg ancestor(s) -- %s already "
                "short (PV already consumed/expired) -- accepted gap, "
                "ADR-0006.",
                order_id,
                len(sufficient_ids),
                len(ancestor_ids),
                leg,
                sorted(short_ids),
            )

    period = purchase_date.replace(day=1)
    personal_affected = MonthlyPersonalPv.objects.filter(
        distributor=distributor, period=period, pv__gte=pv_amount
    ).update(pv=F("pv") - pv_amount)
    if not personal_affected:
        logger.warning(
            "cancel_or_refund_order: order pk=%s MonthlyPersonalPv "
            "reversal for distributor=%s period=%s was a no-op (already "
            "below pv_amount=%s) -- accepted gap, ADR-0006.",
            order_id,
            distributor.pk,
            period,
            pv_amount,
        )


def _send_order_status_notification(order: Order) -> None:
    # Each channel wrapped individually, matching
    # _send_confirmation_notifications's existing convention exactly --
    # stock/PV are already committed by this point, so a notification-
    # provider outage must never look like this cancellation/refund was
    # rolled back. This also runs INSIDE retry_on_lock_contention's
    # caller (_attempt), so an uncaught exception here would propagate
    # out of cancel_or_refund_order entirely after the DB work already
    # committed -- or, in the unlikely case it happens to look like a
    # lock-contention OperationalError, get misread as one and re-run an
    # already-committed transaction a second time.
    try:
        send_sms(
            str(order.phone_number),
            f"Your Bancostore order {order.payment_reference} is now "
            f"{order.get_status_display()}.",
        )
    except Exception:
        logger.exception(
            "cancel_or_refund_order: failed to send SMS for " "reference=%s",
            order.payment_reference,
        )

    if order.email:
        try:
            send_mail(
                subject=f"Your Bancostore order is {order.get_status_display()}",
                message=(
                    f"Your order {order.payment_reference} is now "
                    f"{order.get_status_display()}."
                ),
                from_email=None,
                recipient_list=[order.email],
            )
        except Exception:
            logger.exception(
                "cancel_or_refund_order: failed to send email for " "reference=%s",
                order.payment_reference,
            )
