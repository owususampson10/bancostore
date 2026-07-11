from django.db import transaction

from .models import Product


class InsufficientStockError(Exception):
    pass


def decrement_stock(product: Product, quantity: int = 1) -> Product:
    """Called when a sale completes. This is deliberately the basic
    mechanic only (Task 7's own scope) — the real checkout/order pipeline
    (Phase 6) will call this from within its own transaction once it
    exists. select_for_update() still matters even at this basic scope:
    two concurrent purchases of the last unit in stock must not both
    succeed."""
    if quantity < 1:
        raise ValueError("quantity must be at least 1")

    with transaction.atomic():
        locked_product = Product.objects.select_for_update().get(pk=product.pk)
        if locked_product.stock < quantity:
            raise InsufficientStockError(
                f"Cannot decrement {quantity} from stock of "
                f"{locked_product.stock} for {locked_product.name!r}."
            )
        locked_product.stock -= quantity
        locked_product.save(update_fields=["stock"])

    return locked_product
