from decimal import Decimal

from django.db.models import F, Q

from constance import config

from apps.accounts.permissions import is_distributor
from apps.orders.models import Order

from .models import DiscountCode


class InvalidDiscountCodeError(Exception):
    """Carries a user-facing message -- callers (apps.orders.views) surface
    `str(exc)` directly on the checkout form, matching how every other
    checkout-time rejection in this codebase (Paystack failure, insufficient
    stock) gets a plain message rather than a raw exception leaking through."""


def redeem_discount_code(code_str, *, subtotal, user, phone_number):
    """Task 43b. Validates a customer-submitted code at checkout time and
    returns `(DiscountCode | None, Decimal)` -- the discount object (for
    snapshotting onto the Order) and the exact GHS amount to deduct, already
    capped at `MAX_DISCOUNT_PER_ORDER` and rounded to the pesewa (via
    `DiscountCode.calculate_discount`). Raises `InvalidDiscountCodeError`
    with a user-facing message for any rejection. `code_str` blank/whitespace
    is not an error -- it means "no code entered," returning `(None,
    Decimal("0"))` silently.

    Does NOT touch `times_used` -- that only happens at payment confirmation
    (`consume_discount_code`), matching how stock isn't decremented at order
    creation either, since most PENDING orders never get paid.

    Checks what's knowable at creation time: enabled, exists, active, not
    expired, under the global cap, the customer's real role (Task 43c) if
    the code is audience-restricted, and (if `limit_one_per_customer`) not
    already used by this same identity.

    doubt-driven-development (fresh-context adversarial review before this
    was written, 2026-08-13) found the per-customer check needs to match
    `Order.customer` OR `Order.phone_number` together, regardless of whether
    THIS checkout is guest or logged-in -- a customer who redeemed as a
    guest and later creates an account (or the reverse) must still be
    caught. `phone_number` is always present on every Order (required
    regardless of guest/account status), making it the one identity signal
    guaranteed to exist on both sides.

    Two related design decisions, confirmed directly with the user against
    real-world platform behavior (Shopify/Stripe/Amazon) rather than
    assumed: (1) a code exhausted or disabled/expired AFTER this check
    passes but BEFORE the resulting order's payment confirms is still
    honored at confirmation -- `is_active`/`expiry_date`/`max_uses` are
    never re-checked there, only validated here at creation time. (2) two
    customers racing for the last usage slot: once a payment is captured,
    the order is never cancelled over the race -- see
    `consume_discount_code`."""
    if not code_str or not code_str.strip():
        return None, Decimal("0")

    if not config.DISCOUNT_CODES_ENABLED:
        raise InvalidDiscountCodeError("Discount codes are currently unavailable.")

    # security-and-hardening (2026-08-13): a nonexistent code and a real
    # but expired/inactive/exhausted one deliberately raise the exact same
    # message. Distinguishing them would let an attacker use the response
    # to enumerate which guessed strings are real codes, independent of
    # whether those codes currently work -- an existence oracle. Neither
    # branch reveals which specific check failed.
    try:
        code = DiscountCode.objects.get(code=code_str.strip().upper())
    except DiscountCode.DoesNotExist:
        raise InvalidDiscountCodeError("This discount code is invalid or expired.")

    if not code.is_redeemable():
        raise InvalidDiscountCodeError("This discount code is invalid or expired.")

    # Task 43c. is_distributor(user) is the same real-role check every
    # other role gate in this codebase uses (apps.orders.services already
    # imports it for the PV-crediting branch of confirm_order_payment) --
    # never re-derived from PV or any other order-specific signal. Safe
    # for an unauthenticated guest: Django's AnonymousUser.groups is an
    # EmptyManager, so is_distributor(AnonymousUser()) is False, not an
    # error.
    if code.audience == DiscountCode.Audience.DISTRIBUTOR and not is_distributor(user):
        raise InvalidDiscountCodeError("This discount code is invalid or expired.")
    if code.audience == DiscountCode.Audience.RETAIL and is_distributor(user):
        raise InvalidDiscountCodeError("This discount code is invalid or expired.")

    if code.limit_one_per_customer:
        identity_filter = Q(phone_number=phone_number)
        if user.is_authenticated:
            identity_filter |= Q(customer=user)
        already_used = code.orders.filter(
            identity_filter, status=Order.Status.CONFIRMED
        ).exists()
        if already_used:
            raise InvalidDiscountCodeError("You've already used this discount code.")

    discount_amount = min(
        code.calculate_discount(subtotal), config.MAX_DISCOUNT_PER_ORDER
    )
    return code, discount_amount


def consume_discount_code(discount_code_id):
    """Task 43b. Called inside `confirm_order_payment`'s existing lock, once
    payment is verified. An atomic `F()` update, mirroring
    `apps.catalog.services.decrement_stock`'s conditional-update discipline
    -- except there's no `WHERE times_used < max_uses` guard here, and no
    failure path: the user-confirmed design honors an already-paid order
    unconditionally, even if this pushes `times_used` past `max_uses` in the
    rare case where a concurrent order already claimed the last slot. See
    `redeem_discount_code`'s docstring for the real-world precedent this
    follows."""
    DiscountCode.objects.filter(pk=discount_code_id).update(
        times_used=F("times_used") + 1
    )


def release_discount_code(discount_code_id):
    """Task 43b. The symmetric reversal of `consume_discount_code`, called
    inside `cancel_or_refund_order`'s existing lock for a CONFIRMED order
    being cancelled/refunded -- mirrors `apps.catalog.services.
    increment_stock`'s existing symmetry with `decrement_stock`. Floored at
    0 via the `times_used__gt=0` filter (never a negative count), matching
    this codebase's established defensive-update convention. Freeing the
    slot also naturally re-opens `limit_one_per_customer` for this
    customer, since that check only looks at CONFIRMED orders -- correct,
    not a bypass: a cancelled/refunded purchase shouldn't have permanently
    spent the customer's one use."""
    DiscountCode.objects.filter(pk=discount_code_id, times_used__gt=0).update(
        times_used=F("times_used") - 1
    )
