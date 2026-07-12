from decimal import Decimal

from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product, ProductVariant


@pytest.mark.django_db
def test_staff_can_create_a_product_via_django_admin(staff_client):
    category = Category.objects.create(name="Watches", slug="watches")

    response = staff_client.post(
        reverse("admin:catalog_product_add"),
        {
            "name": "Classic Steel Watch",
            "slug": "classic-steel-watch",
            "description": "A timeless design.",
            "category": category.id,
            "price": "1500.00",
            "pv_value": "500",
            "stock": "10",
            "is_active": "on",
            "is_featured": "on",
            # Required management-form fields for the ProductImageInline and
            # ProductVariantInline, even when adding zero rows through them.
            "images-TOTAL_FORMS": "0",
            "images-INITIAL_FORMS": "0",
            "images-MIN_NUM_FORMS": "0",
            "images-MAX_NUM_FORMS": "1000",
            "variants-TOTAL_FORMS": "0",
            "variants-INITIAL_FORMS": "0",
            "variants-MIN_NUM_FORMS": "0",
            "variants-MAX_NUM_FORMS": "1000",
        },
    )

    assert response.status_code == 302
    product = Product.objects.get(name="Classic Steel Watch")
    assert product.category == category
    assert product.price == Decimal("1500.00")
    assert product.pv_value == 500
    assert product.stock == 10
    assert product.is_active is True
    assert product.is_featured is True


@pytest.mark.django_db
def test_product_pv_value_is_separate_from_price():
    """SPEC.md Section 1.4: PV is only relevant to distributor purchases and
    is never derived from price — a cheap product can carry high PV and
    vice versa, so they must be independently settable."""
    category = Category.objects.create(name="Jewellery", slug="jewellery")
    product = Product.objects.create(
        name="Gold Ring",
        category=category,
        price=Decimal("50.00"),
        pv_value=1000,
        stock=5,
    )

    assert product.price == Decimal("50.00")
    assert product.pv_value == 1000


@pytest.mark.django_db
def test_product_in_stock_property():
    category = Category.objects.create(name="Perfumes", slug="perfumes")
    in_stock = Product.objects.create(
        name="Oud Perfume", category=category, price=Decimal("80.00"), stock=3
    )
    out_of_stock = Product.objects.create(
        name="Rose Perfume", category=category, price=Decimal("80.00"), stock=0
    )

    assert in_stock.in_stock is True
    assert out_of_stock.in_stock is False


@pytest.mark.django_db
def test_product_can_have_multiple_variants():
    category = Category.objects.create(name="Watches", slug="watches")
    product = Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("1500.00"), stock=10
    )

    ProductVariant.objects.create(product=product, name="Colour", value="Silver")
    ProductVariant.objects.create(product=product, name="Colour", value="Gold")

    assert product.variants.count() == 2
    assert {v.value for v in product.variants.all()} == {"Silver", "Gold"}


@pytest.mark.django_db
def test_product_changelist_does_not_n_plus_one_on_category(staff_client):
    """ProductAdmin.list_display includes "category" — without
    list_select_related, the changelist issues one extra query per row to
    resolve str(product.category). Compares query count at two product
    counts rather than asserting an absolute number, since silk's
    middleware (active locally under DEBUG=True) adds its own DB writes on
    every request and would make a fixed threshold flaky."""
    from django.db import connection, reset_queries
    from django.test.utils import CaptureQueriesContext

    category = Category.objects.create(name="Watches", slug="watches")
    url = reverse("admin:catalog_product_changelist")

    Product.objects.create(
        name="Watch 1", category=category, price=Decimal("1500.00"), stock=1
    )
    reset_queries()
    with CaptureQueriesContext(connection) as one_product:
        response = staff_client.get(url)
    assert response.status_code == 200

    for i in range(2, 7):
        Product.objects.create(
            name=f"Watch {i}", category=category, price=Decimal("1500.00"), stock=1
        )
    reset_queries()
    with CaptureQueriesContext(connection) as six_products:
        response = staff_client.get(url)
    assert response.status_code == 200

    # Adding 5 more products must not add ~5 more queries (the N+1
    # signature) — it should add none, since select_related fetches every
    # product's category in the same query as the product itself. A
    # tolerance of 2 (not exact equality) absorbs silk's own occasional
    # internal housekeeping query (e.g. checking whether to prune old
    # silk_request rows) — confirmed via an actual query diff to be
    # unrelated to product count, not guessed. An N+1 regression would add
    # 5 queries, not 1-2, so this still catches the real failure mode.
    assert len(six_products) <= len(one_product) + 2
