import threading
from decimal import Decimal

from django.db import connection

import pytest

from apps.catalog.models import Category, Product
from apps.catalog.services import (
    InsufficientStockError,
    decrement_stock,
    increment_stock,
)


@pytest.fixture
def product():
    category = Category.objects.create(name="Watches", slug="watches")
    return Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("1500.00"), stock=10
    )


@pytest.mark.django_db
def test_decrement_stock_reduces_the_count(product):
    decrement_stock(product, quantity=3)

    product.refresh_from_db()
    assert product.stock == 7


@pytest.mark.django_db
def test_decrement_stock_defaults_to_one(product):
    decrement_stock(product)

    product.refresh_from_db()
    assert product.stock == 9


@pytest.mark.django_db
def test_decrement_stock_raises_when_insufficient(product):
    with pytest.raises(InsufficientStockError):
        decrement_stock(product, quantity=11)

    product.refresh_from_db()
    assert product.stock == 10


@pytest.mark.django_db
def test_decrement_stock_rejects_a_non_positive_quantity(product):
    with pytest.raises(ValueError):
        decrement_stock(product, quantity=0)


@pytest.mark.django_db
def test_increment_stock_increases_the_count(product):
    increment_stock(product, quantity=3)

    product.refresh_from_db()
    assert product.stock == 13


@pytest.mark.django_db
def test_increment_stock_rejects_a_non_positive_quantity(product):
    with pytest.raises(ValueError):
        increment_stock(product, quantity=0)

    product.refresh_from_db()
    assert product.stock == 10


@pytest.mark.django_db(transaction=True)
def test_concurrent_increments_never_lose_a_unit():
    """Mirrors test_concurrent_decrements_never_oversell_the_last_unit's own
    reasoning: this only genuinely proves the locking itself once run
    against real MySQL in CI (SQLite drops FOR UPDATE), but still catches a
    regression in the surrounding lock/transaction wrapper on SQLite too."""
    category = Category.objects.create(name="Watches", slug="watches")
    product = Product.objects.create(
        name="Restocked Watch", category=category, price=Decimal("1500.00"), stock=0
    )

    lock = threading.Lock()
    outcomes = []

    def attempt():
        increment_stock(product, quantity=1)
        connection.close()
        with lock:
            outcomes.append("done")

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outcomes) == 5
    product.refresh_from_db()
    assert product.stock == 5


@pytest.mark.django_db(transaction=True)
def test_concurrent_decrements_never_oversell_the_last_unit():
    """decrement_stock uses select_for_update() specifically so two
    concurrent purchases of the last unit can't both succeed. This only
    genuinely proves that under a database that supports row locking —
    connection.features.has_select_for_update is False on SQLite, so
    Django silently drops the FOR UPDATE clause there and this test cannot
    prove the locking itself works locally (same constraint CLAUDE.md notes
    for commission/wallet/PV-ledger tests: they need real MySQL in CI).
    Keeping the test anyway: it's meaningful once run against MySQL, and it
    still catches a regression in the surrounding logic (e.g. someone
    removing the lock/transaction wrapper entirely) even on SQLite, since
    each thread gets its own connection here rather than sharing state."""
    category = Category.objects.create(name="Watches", slug="watches")
    product = Product.objects.create(
        name="Limited Watch", category=category, price=Decimal("1500.00"), stock=1
    )

    outcomes = []
    lock = threading.Lock()

    def attempt():
        try:
            decrement_stock(product, quantity=1)
            outcome = "success"
        except InsufficientStockError:
            outcome = "insufficient"
        finally:
            connection.close()  # each thread must not share the main
            # thread's connection/transaction state
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("success") == 1
    assert outcomes.count("insufficient") == 4

    product.refresh_from_db()
    assert product.stock == 0
