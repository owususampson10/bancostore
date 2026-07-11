from decimal import Decimal

import pytest

from apps.catalog.models import Category, Product
from apps.catalog.services import InsufficientStockError, decrement_stock


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
