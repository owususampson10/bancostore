from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product
from apps.orders.models import Order

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


def _add_to_cart(client, product):
    client.post(reverse("orders:cart_add", args=[product.pk]))


def _valid_home_delivery_data(**overrides):
    data = {
        "full_name": "Guest Shopper",
        "phone_number": "+233241234567",
        "email": "",
        "delivery_method": Order.DeliveryMethod.HOME_DELIVERY,
        "delivery_zone": Order.DeliveryZone.ACCRA,
        "address": "12 High St",
        "area": "Osu",
        "landmark": "",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_checkout_redirects_to_cart_when_the_cart_is_empty(client):
    response = client.get(reverse("orders:checkout"))
    assert response.status_code == 302
    assert response.url == reverse("orders:cart")


@pytest.mark.django_db
def test_checkout_page_renders_with_items_in_the_cart(client):
    product = _make_product()
    _add_to_cart(client, product)

    response = client.get(reverse("orders:checkout"))

    assert response.status_code == 200
    assert product.name in response.content.decode()


@pytest.mark.django_db
def test_guest_checkout_form_starts_blank(client):
    product = _make_product()
    _add_to_cart(client, product)

    response = client.get(reverse("orders:checkout"))

    assert response.context["form"].initial == {}


@pytest.mark.django_db
def test_logged_in_customer_gets_contact_info_prefilled(client):
    from apps.accounts.models import CustomerProfile

    user = User.objects.create_user(
        username="+233551234567", password="Passw0rd!", email="kofi@example.test"
    )
    CustomerProfile.objects.create(
        user=user, full_name="Kofi Customer", phone_number="+233551234567"
    )
    client.force_login(user)
    product = _make_product()
    _add_to_cart(client, product)

    response = client.get(reverse("orders:checkout"))

    assert response.context["form"].initial["full_name"] == "Kofi Customer"


@pytest.mark.django_db
def test_submitting_valid_checkout_creates_a_pending_order(client):
    product = _make_product()
    _add_to_cart(client, product)

    response = client.post(reverse("orders:checkout"), _valid_home_delivery_data())

    # Post/Redirect/Get (code-review-and-quality, 2026-07-25): a page
    # refresh on a directly-rendered response would resubmit the form and
    # create a duplicate order.
    order = Order.objects.get()
    assert response.status_code == 302
    assert response.url == reverse(
        "orders:order_confirmation", args=[order.payment_reference]
    )
    assert order.status == Order.Status.PENDING
    assert order.customer is None
    assert order.items.count() == 1


@pytest.mark.django_db
def test_submitting_valid_checkout_as_a_logged_in_user_links_the_order_to_the_account(
    client,
):
    user = User.objects.create_user(username="+233551234568", password="Passw0rd!")
    client.force_login(user)
    product = _make_product()
    _add_to_cart(client, product)

    client.post(reverse("orders:checkout"), _valid_home_delivery_data())

    order = Order.objects.get()
    assert order.customer_id == user.pk


@pytest.mark.django_db
def test_missing_required_home_delivery_fields_does_not_create_an_order(client):
    product = _make_product()
    _add_to_cart(client, product)

    response = client.post(
        reverse("orders:checkout"),
        _valid_home_delivery_data(address="", area="", delivery_zone=""),
    )

    assert response.status_code == 200
    assert not Order.objects.exists()
    assert "This field is required for home delivery." in response.content.decode()


@pytest.mark.django_db
def test_pickup_checkout_does_not_require_address_fields(client):
    product = _make_product()
    _add_to_cart(client, product)

    response = client.post(
        reverse("orders:checkout"),
        _valid_home_delivery_data(
            delivery_method=Order.DeliveryMethod.PICKUP,
            delivery_zone="",
            address="",
            area="",
        ),
    )

    assert response.status_code == 302
    order = Order.objects.get()
    assert order.delivery_method == Order.DeliveryMethod.PICKUP
    assert order.delivery_fee == Decimal("0")


@pytest.mark.django_db
def test_pickup_discards_delivery_zone_address_and_area_even_if_submitted(client):
    # code-review-and-quality (2026-07-25): Alpine only hides the delivery-
    # details fields via x-show when pickup is selected -- it doesn't
    # clear their values, so a user who filled in home-delivery fields
    # before switching to pickup would still submit them. CheckoutForm
    # .clean() must discard them regardless, matching Order's own
    # CheckConstraint (pickup implies blank) -- unproven by any existing
    # test until now, since every other pickup test already posts blank
    # values.
    product = _make_product()
    _add_to_cart(client, product)

    client.post(
        reverse("orders:checkout"),
        _valid_home_delivery_data(delivery_method=Order.DeliveryMethod.PICKUP),
    )

    order = Order.objects.get()
    assert order.delivery_method == Order.DeliveryMethod.PICKUP
    assert order.delivery_zone == ""
    assert order.address == ""
    assert order.area == ""
    assert order.delivery_fee == Decimal("0")


@pytest.mark.django_db
def test_checkout_post_with_a_since_emptied_cart_redirects_to_cart(client):
    product = _make_product()
    _add_to_cart(client, product)
    client.post(reverse("orders:cart_remove", args=[product.pk]))

    response = client.post(reverse("orders:checkout"), _valid_home_delivery_data())

    assert response.status_code == 302
    assert response.url == reverse("orders:cart")
    assert not Order.objects.exists()
