import uuid
from decimal import Decimal

from django.db import transaction

from constance import config

from apps.accounts.permissions import is_distributor

from .models import Order, OrderItem


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
