from decimal import Decimal

import pytest

from apps.catalog.models import Category, Product, ProductImage
from apps.catalog.services import normalize_primary_image


@pytest.fixture
def product():
    category = Category.objects.create(name="Watches", slug="watches")
    return Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("1500.00"), stock=10
    )


@pytest.mark.django_db
def test_no_images_is_a_noop(product):
    normalize_primary_image(product)  # must not raise


@pytest.mark.django_db
def test_a_single_primary_image_stays_unchanged(product):
    image = ProductImage.objects.create(product=product, is_primary=True, order=0)

    normalize_primary_image(product)

    image.refresh_from_db()
    assert image.is_primary is True


@pytest.mark.django_db
def test_no_primary_image_promotes_the_first_by_order(product):
    second = ProductImage.objects.create(product=product, is_primary=False, order=1)
    first = ProductImage.objects.create(product=product, is_primary=False, order=0)

    normalize_primary_image(product)

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.is_primary is True
    assert second.is_primary is False


@pytest.mark.django_db
def test_multiple_primary_images_keeps_only_the_first_by_order(product):
    first = ProductImage.objects.create(product=product, is_primary=True, order=0)
    second = ProductImage.objects.create(product=product, is_primary=True, order=1)

    normalize_primary_image(product)

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.is_primary is True
    assert second.is_primary is False
