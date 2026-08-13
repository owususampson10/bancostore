from django.db import transaction

from constance import config

from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

from .models import Product


class InsufficientStockError(Exception):
    pass


def decrement_stock(
    product: Product, quantity: int = 1, *, allow_backorder: bool = False
) -> tuple[Product, int]:
    """Called when a sale completes. This is deliberately the basic
    mechanic only (Task 7's own scope) — the real checkout/order pipeline
    (Phase 6) will call this from within its own transaction once it
    exists. select_for_update() still matters even at this basic scope:
    two concurrent purchases of the last unit in stock must not both
    succeed. See bancostore/concurrency.py for why
    retry_on_lock_contention/select_for_update_nowait_if_supported are
    needed on top of select_for_update() alone.

    Task 44b: allow_backorder=True clamps the decrement at whatever stock
    is actually on hand (down to zero, never negative — Product.stock is
    a PositiveIntegerField with its own DB-level CHECK constraint, so
    going negative isn't just undesirable, it's impossible) instead of
    raising InsufficientStockError. Only ever pass True from a value
    that's ALREADY been snapshotted somewhere (Order.
    backorders_allowed_at_checkout, Task 44b) — never a live constance
    re-read at this call site, per that field's own docstring: a
    doubt-driven-development review found a live re-read here could
    wrongly cancel an order the customer already paid for, if an admin
    disables backorders between Paystack capturing payment and a delayed
    webhook/callback actually reaching confirm_order_payment.

    Returns (locked_product, actual_quantity_decremented) — the second
    element is `quantity` unless allow_backorder clamped it, in which
    case it's whatever was really removed from stock. A caller that
    doesn't need to distinguish the two (every existing caller before
    Task 44b) can simply ignore it."""
    if quantity < 1:
        raise ValueError("quantity must be at least 1")

    def _attempt():
        with transaction.atomic():
            locked_product = select_for_update_nowait_if_supported(Product.objects).get(
                pk=product.pk
            )
            if locked_product.stock < quantity:
                if not allow_backorder:
                    raise InsufficientStockError(
                        f"Cannot decrement {quantity} from stock of "
                        f"{locked_product.stock} for {locked_product.name!r}."
                    )
                actual_decremented = locked_product.stock
                locked_product.stock = 0
            else:
                actual_decremented = quantity
                locked_product.stock -= quantity
            locked_product.save(update_fields=["stock"])
        return locked_product, actual_decremented

    return retry_on_lock_contention(_attempt)


def normalize_primary_image(product: Product) -> None:
    """Admin Catalog Management: the image formset lets an admin (re)mark
    any row as primary, or mark none, or mark several at once (unlike a
    radio button, independent checkboxes give no client-side guarantee).
    Called once after every product-form save so `Product.primary_image`
    (which just returns the first `is_primary=True` row it finds) always
    has exactly one true candidate: promotes the first image by `order`
    if none is marked, and demotes every image but the first-by-order one
    if more than one is marked."""
    images = list(product.images.all())
    if not images:
        return

    primary_images = [image for image in images if image.is_primary]
    if len(primary_images) == 1:
        return

    for image in images:
        should_be_primary = image.pk == images[0].pk
        if image.is_primary != should_be_primary:
            image.is_primary = should_be_primary
            image.save(update_fields=["is_primary"])


def increment_stock(product: Product, quantity: int = 1) -> Product:
    """Symmetric inverse of decrement_stock, for order cancellation/refund
    reversal (Task 18b). No InsufficientStockError equivalent -- there is
    no upper bound to violate."""
    if quantity < 1:
        raise ValueError("quantity must be at least 1")

    def _attempt():
        with transaction.atomic():
            locked_product = select_for_update_nowait_if_supported(Product.objects).get(
                pk=product.pk
            )
            locked_product.stock += quantity
            locked_product.save(update_fields=["stock"])
        return locked_product

    return retry_on_lock_contention(_attempt)


def is_backorder_eligible(product: Product) -> bool:
    """Task 44a. True only for an out-of-stock product while both
    BACKORDERS_ENABLED (the master switch) and OUT_OF_STOCK_BEHAVIOUR
    ("backorder") agree -- a global setting, not per-product (design
    confirmed directly with the user against the primary source doc's own
    literal wording, reversing this task's original per-product plan).
    Always False for an in-stock product: there's nothing to "backorder"."""
    if product.stock > 0:
        return False
    return config.BACKORDERS_ENABLED and config.OUT_OF_STOCK_BEHAVIOUR == "backorder"


def backorder_display_context() -> dict:
    """The two pieces of context every storefront template needs to
    decide whether an out-of-stock product shows 'Add to Cart' with the
    admin's configured message instead of 'Out of Stock'. A single global
    setting, so this never varies by which product is being rendered --
    callers add it to their template context dict once per view, not
    per-product."""
    return {
        "backorders_active": (
            config.BACKORDERS_ENABLED and config.OUT_OF_STOCK_BEHAVIOUR == "backorder"
        ),
        "backorder_message": config.BACKORDER_MESSAGE,
    }


def storefront_visible_products():
    """Task 44a. Wraps `Product.objects.storefront_visible()` with the
    live OUT_OF_STOCK_BEHAVIOUR setting applied -- a no-op unless an admin
    has set it to "hide", in which case an out-of-stock product is
    excluded everywhere a storefront view uses this (listing, search,
    home, product detail, cart_add), matching "Hide product" literally:
    gone everywhere, not just visually. Lives here rather than as a
    `ProductQuerySet` method on `Product` itself so `apps/catalog/models.py`
    never needs to import constance -- this codebase's models stay free of
    runtime-config coupling everywhere else, and this is the one queryset
    a live setting needs to shape."""
    queryset = Product.objects.storefront_visible()
    if config.OUT_OF_STOCK_BEHAVIOUR == "hide":
        queryset = queryset.exclude(stock=0)
    return queryset
