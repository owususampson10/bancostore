from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection, reset_queries
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product

User = get_user_model()


def _make_product(**overrides):
    category, _ = Category.objects.get_or_create(name="Wellness", slug="wellness")
    defaults = {
        "name": "Vitality Pulse Smart Ring",
        "category": category,
        "price": Decimal("450.00"),
        "pv_value": 60,
        "stock": 5,
        "is_active": True,
    }
    defaults.update(overrides)
    return Product.objects.create(**defaults)


@pytest.mark.django_db
def test_cart_page_renders_empty_state_with_no_items(client):
    response = client.get(reverse("orders:cart"))
    assert response.status_code == 200
    assert "empty" in response.content.decode().lower()


@pytest.mark.django_db
def test_add_to_cart_then_cart_page_shows_the_item(client):
    product = _make_product()

    client.post(reverse("orders:cart_add", args=[product.pk]))
    response = client.get(reverse("orders:cart"))

    assert response.status_code == 200
    assert product.name in response.content.decode()


@pytest.mark.django_db
def test_add_to_cart_works_for_a_logged_in_user_too(client):
    product = _make_product()
    user = User.objects.create_user(username="shopper", password="Passw0rd!")
    client.force_login(user)

    client.post(reverse("orders:cart_add", args=[product.pk]))
    response = client.get(reverse("orders:cart"))

    assert product.name in response.content.decode()


@pytest.mark.django_db
def test_add_to_cart_rejects_get_requests(client):
    product = _make_product()
    response = client.get(reverse("orders:cart_add", args=[product.pk]))
    assert response.status_code == 405


@pytest.mark.django_db
def test_add_to_cart_404s_for_an_inactive_product(client):
    product = _make_product(is_active=False)
    response = client.post(reverse("orders:cart_add", args=[product.pk]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_add_to_cart_redirects_to_the_product_page_not_the_cart_when_stock_is_exhausted(
    client,
):
    # code-review-and-quality (2026-07-24): redirecting to /cart/ here
    # would silently imply success even though nothing was added.
    product = _make_product(stock=0, is_active=True)
    # storefront_visible() only requires is_active=True -- stock=0 products
    # are still resolvable (they show an "Out of Stock" label, per Task 7),
    # so this POST can still reach cart_add's stock-exhausted branch.
    response = client.post(reverse("orders:cart_add", args=[product.pk]))
    assert response.url == reverse("catalog:product_detail", args=[product.slug])

    cart_response = client.get(reverse("orders:cart"))
    assert product.name not in cart_response.content.decode()


@pytest.mark.django_db
def test_update_does_not_404_for_a_product_deactivated_after_being_added(client):
    # code-review-and-quality (2026-07-24): cart_update previously used
    # Product.objects.storefront_visible() (is_active=True), 404ing this
    # action the moment a cart product was deactivated -- a dead end the
    # customer couldn't recover from. Fixed by using the plain manager
    # here, so a stale page/direct POST for a since-deactivated product
    # still redirects cleanly instead of erroring.
    #
    # doubt-driven-development (Task 17c design review) then found
    # Cart.items() itself didn't exclude deactivated products either --
    # fixed separately (test_cart.py), which changes this test's own
    # expected outcome: the line self-heals out of the cart entirely
    # (not just "doesn't 404"), since a deactivated product is exactly as
    # unpurchasable as a deleted one.
    product = _make_product(stock=10)
    client.post(reverse("orders:cart_add", args=[product.pk]))
    product.is_active = False
    product.save()

    response = client.post(
        reverse("orders:cart_update", args=[product.pk]), {"quantity": 2}
    )

    assert response.status_code == 302
    cart_response = client.get(reverse("orders:cart"))
    assert cart_response.context["cart_items"] == []


@pytest.mark.django_db
def test_remove_removes_a_line_for_a_product_deactivated_after_being_added(client):
    product = _make_product(stock=10)
    client.post(reverse("orders:cart_add", args=[product.pk]))
    product.is_active = False
    product.save()

    response = client.post(reverse("orders:cart_remove", args=[product.pk]))

    assert response.status_code == 302
    cart_response = client.get(reverse("orders:cart"))
    assert product.name not in cart_response.content.decode()


@pytest.mark.django_db
def test_cart_page_query_count_does_not_grow_with_item_count(client):
    """Compares query count at two cart sizes rather than asserting an
    absolute number -- see test_listing.py's
    test_grid_query_count_does_not_grow_with_product_count for why (silk's
    middleware, active locally under DEBUG=True, adds its own DB writes on
    every request and would make a fixed threshold flaky)."""
    for i in range(2):
        product = _make_product(name=f"Small batch {i}", stock=10)
        client.post(reverse("orders:cart_add", args=[product.pk]))
    reset_queries()
    with CaptureQueriesContext(connection) as small_cart:
        client.get(reverse("orders:cart"))

    client.session.flush()
    for i in range(8):
        product = _make_product(name=f"Large batch {i}", stock=10)
        client.post(reverse("orders:cart_add", args=[product.pk]))
    reset_queries()
    with CaptureQueriesContext(connection) as large_cart:
        client.get(reverse("orders:cart"))

    assert len(large_cart.captured_queries) <= len(small_cart.captured_queries) + 2


@pytest.mark.django_db
def test_update_quantity_changes_the_cart(client):
    product = _make_product(stock=10)
    client.post(reverse("orders:cart_add", args=[product.pk]))

    client.post(reverse("orders:cart_update", args=[product.pk]), {"quantity": 4})

    response = client.get(reverse("orders:cart"))
    assert "GHS 1,800.00" in response.content.decode()


@pytest.mark.django_db
def test_update_quantity_to_zero_removes_the_item(client):
    product = _make_product()
    client.post(reverse("orders:cart_add", args=[product.pk]))

    client.post(reverse("orders:cart_update", args=[product.pk]), {"quantity": 0})

    response = client.get(reverse("orders:cart"))
    assert product.name not in response.content.decode()


@pytest.mark.django_db
def test_remove_removes_the_item_from_the_cart(client):
    product = _make_product()
    client.post(reverse("orders:cart_add", args=[product.pk]))

    client.post(reverse("orders:cart_remove", args=[product.pk]))

    response = client.get(reverse("orders:cart"))
    assert product.name not in response.content.decode()


@pytest.mark.django_db
def test_cart_badge_count_reflects_items_across_pages(client):
    product = _make_product(stock=10)
    client.post(reverse("orders:cart_add", args=[product.pk]))
    client.post(reverse("orders:cart_update", args=[product.pk]), {"quantity": 3})

    response = client.get(reverse("catalog:home"))

    assert response.context["cart_count"] == 3
