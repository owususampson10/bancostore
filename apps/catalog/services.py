import time

from django.db import OperationalError, transaction

from .models import Product

MAX_LOCK_RETRIES = 10
RETRY_BACKOFF_SECONDS = 0.05


class InsufficientStockError(Exception):
    pass


def _is_lock_contention_error(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return (
        "is locked" in message  # SQLite: "database is locked" /
        # "database table is locked" — matches either without guessing
        # which exact phrasing a given SQLite version uses.
        or "lock wait timeout" in message  # MySQL
        or "deadlock" in message  # MySQL
    )


def decrement_stock(product: Product, quantity: int = 1) -> Product:
    """Called when a sale completes. This is deliberately the basic
    mechanic only (Task 7's own scope) — the real checkout/order pipeline
    (Phase 6) will call this from within its own transaction once it
    exists. select_for_update() still matters even at this basic scope:
    two concurrent purchases of the last unit in stock must not both
    succeed.

    Real concurrent access raises OperationalError from the database
    itself while a competing transaction holds the lock — "database is
    locked" on SQLite, lock-wait-timeout or deadlock on MySQL. This wasn't
    theoretical: a real multi-threaded test against local SQLite
    reproduced it directly (see tests/feature/catalog/test_stock.py::
    test_concurrent_decrements_never_oversell_the_last_unit). Retrying a
    bounded number of times with a short backoff turns "this purchase
    crashed" into "this purchase briefly waited its turn." """
    if quantity < 1:
        raise ValueError("quantity must be at least 1")

    attempt = 0
    while True:
        attempt += 1
        try:
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
        except OperationalError as exc:
            if attempt >= MAX_LOCK_RETRIES or not _is_lock_contention_error(exc):
                raise
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
