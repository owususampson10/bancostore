from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest
from constance import config

from apps.catalog.models import Category, Product
from apps.catalog.services import (
    backorder_display_context,
    is_backorder_eligible,
    storefront_visible_products,
)

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


@pytest.fixture(autouse=True)
def _reset_backorder_settings():
    """Every test starts from the real seeded defaults (BACKORDERS_ENABLED
    Off, OUT_OF_STOCK_BEHAVIOUR "show") -- tests that need a different mode
    set it explicitly, matching this codebase's own established
    save/restore convention for constance overrides in tests."""
    original_enabled = config.BACKORDERS_ENABLED
    original_behaviour = config.OUT_OF_STOCK_BEHAVIOUR
    original_message = config.BACKORDER_MESSAGE
    yield
    config.BACKORDERS_ENABLED = original_enabled
    config.OUT_OF_STOCK_BEHAVIOUR = original_behaviour
    config.BACKORDER_MESSAGE = original_message


# ---------------------------------------------------------------------------
# apps.catalog.services helpers
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_is_backorder_eligible_is_false_for_an_in_stock_product():
    config.BACKORDERS_ENABLED = True
    config.OUT_OF_STOCK_BEHAVIOUR = "backorder"
    product = _make_product(stock=5)

    assert is_backorder_eligible(product) is False


@pytest.mark.django_db
def test_is_backorder_eligible_is_true_when_mode_is_backorder_and_enabled():
    config.BACKORDERS_ENABLED = True
    config.OUT_OF_STOCK_BEHAVIOUR = "backorder"
    product = _make_product(stock=0)

    assert is_backorder_eligible(product) is True


@pytest.mark.django_db
def test_is_backorder_eligible_is_false_when_master_switch_is_off():
    """BACKORDERS_ENABLED is the master switch -- OUT_OF_STOCK_BEHAVIOUR
    set to "backorder" alone must not silently offer backorders."""
    config.BACKORDERS_ENABLED = False
    config.OUT_OF_STOCK_BEHAVIOUR = "backorder"
    product = _make_product(stock=0)

    assert is_backorder_eligible(product) is False


@pytest.mark.django_db
def test_is_backorder_eligible_is_false_when_mode_is_show():
    config.BACKORDERS_ENABLED = True
    config.OUT_OF_STOCK_BEHAVIOUR = "show"
    product = _make_product(stock=0)

    assert is_backorder_eligible(product) is False


@pytest.mark.django_db
def test_backorder_display_context_reflects_live_settings():
    config.BACKORDERS_ENABLED = True
    config.OUT_OF_STOCK_BEHAVIOUR = "backorder"
    config.BACKORDER_MESSAGE = "Ships in 7 days"

    context = backorder_display_context()

    assert context == {
        "backorders_active": True,
        "backorder_message": "Ships in 7 days",
    }


@pytest.mark.django_db
def test_storefront_visible_products_includes_out_of_stock_by_default():
    product = _make_product(stock=0)

    assert product in storefront_visible_products()


@pytest.mark.django_db
def test_storefront_visible_products_excludes_out_of_stock_when_hidden():
    config.OUT_OF_STOCK_BEHAVIOUR = "hide"
    product = _make_product(stock=0)

    assert product not in storefront_visible_products()


@pytest.mark.django_db
def test_storefront_visible_products_still_includes_in_stock_when_hide_mode_is_on():
    config.OUT_OF_STOCK_BEHAVIOUR = "hide"
    product = _make_product(stock=5)

    assert product in storefront_visible_products()


# ---------------------------------------------------------------------------
# Storefront views
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_product_list_excludes_a_hidden_out_of_stock_product(client):
    config.OUT_OF_STOCK_BEHAVIOUR = "hide"
    product = _make_product(stock=0, name="Ghost Product")

    response = client.get(reverse("catalog:product_list"))

    assert product.name not in response.content.decode()


@pytest.mark.django_db
def test_product_detail_404s_for_a_hidden_out_of_stock_product(client):
    config.OUT_OF_STOCK_BEHAVIOUR = "hide"
    product = _make_product(stock=0)

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    assert response.status_code == 404


@pytest.mark.django_db
def test_product_detail_shows_out_of_stock_by_default(client):
    product = _make_product(stock=0)

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    content = response.content.decode()
    assert response.status_code == 200
    assert "Out of Stock" in content
    assert "Add to Cart" not in content


@pytest.mark.django_db
def test_product_detail_shows_backorder_message_and_add_to_cart_when_eligible(client):
    config.BACKORDERS_ENABLED = True
    config.OUT_OF_STOCK_BEHAVIOUR = "backorder"
    config.BACKORDER_MESSAGE = "Ships in 7 days"
    product = _make_product(stock=0)

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    content = response.content.decode()
    assert response.status_code == 200
    assert "Ships in 7 days" in content
    assert "Add to Cart" in content


@pytest.mark.django_db
def test_wishlist_shows_backorder_message_when_eligible(client):
    config.BACKORDERS_ENABLED = True
    config.OUT_OF_STOCK_BEHAVIOUR = "backorder"
    config.BACKORDER_MESSAGE = "Ships in 7 days"
    user = User.objects.create_user(username="shopper", password="Passw0rd!")
    client.force_login(user)
    product = _make_product(stock=0)
    client.post(reverse("catalog:wishlist_toggle", args=[product.pk]))

    response = client.get(reverse("catalog:wishlist"))

    assert "Ships in 7 days" in response.content.decode()


# ---------------------------------------------------------------------------
# cart_add view (Cart.add()/Cart.update() themselves are covered directly
# in tests/unit/orders/test_cart.py)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_cart_add_view_succeeds_for_a_backorder_eligible_product(client):
    config.BACKORDERS_ENABLED = True
    config.OUT_OF_STOCK_BEHAVIOUR = "backorder"
    product = _make_product(stock=0)

    response = client.post(reverse("orders:cart_add", args=[product.pk]))

    assert response.url == reverse("orders:cart")
    cart_response = client.get(reverse("orders:cart"))
    assert product.name in cart_response.content.decode()


@pytest.mark.django_db
def test_cart_page_does_not_block_increasing_a_backorder_eligible_item(client):
    """Regression guard: Cart.add()/update() already allowed a
    backorder-eligible item's quantity to exceed live stock, but
    templates/orders/cart.html's own `quantity >= stock` check didn't know
    that -- it showed "Only 0 left in stock" and disabled the quantity
    "+" button for every backorder item, silently blocking the one thing
    this feature is for. Caught via live-browser verification, not pytest
    alone (the disabled-button behavior itself needs a real click to see,
    but the misleading stock message and disabled attribute are both
    checked here)."""
    config.BACKORDERS_ENABLED = True
    config.OUT_OF_STOCK_BEHAVIOUR = "backorder"
    product = _make_product(stock=0)
    client.post(reverse("orders:cart_add", args=[product.pk]))

    response = client.get(reverse("orders:cart"))

    content = response.content.decode()
    assert "Only 0 left in stock" not in content
    assert 'aria-label="Increase quantity" disabled' not in content
