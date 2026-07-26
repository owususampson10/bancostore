from django.db import transaction

from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

from .models import Product


class InsufficientStockError(Exception):
    pass


def decrement_stock(product: Product, quantity: int = 1) -> Product:
    """Called when a sale completes. This is deliberately the basic
    mechanic only (Task 7's own scope) — the real checkout/order pipeline
    (Phase 6) will call this from within its own transaction once it
    exists. select_for_update() still matters even at this basic scope:
    two concurrent purchases of the last unit in stock must not both
    succeed. See bancostore/concurrency.py for why
    retry_on_lock_contention/select_for_update_nowait_if_supported are
    needed on top of select_for_update() alone."""
    if quantity < 1:
        raise ValueError("quantity must be at least 1")

    def _attempt():
        with transaction.atomic():
            locked_product = select_for_update_nowait_if_supported(Product.objects).get(
                pk=product.pk
            )
            if locked_product.stock < quantity:
                raise InsufficientStockError(
                    f"Cannot decrement {quantity} from stock of "
                    f"{locked_product.stock} for {locked_product.name!r}."
                )
            locked_product.stock -= quantity
            locked_product.save(update_fields=["stock"])
        return locked_product

    return retry_on_lock_contention(_attempt)


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
