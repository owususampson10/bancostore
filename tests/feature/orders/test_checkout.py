from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product
from apps.orders.models import Order

User = get_user_model()

_FAKE_AUTHORIZATION_URL = "https://checkout.paystack.com/fake-access-code"


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
@patch("apps.orders.views.initialize_transaction")
def test_submitting_valid_checkout_creates_a_pending_order_and_redirects_to_paystack(
    mock_initialize, client
):
    mock_initialize.return_value = {"authorization_url": _FAKE_AUTHORIZATION_URL}
    product = _make_product()
    _add_to_cart(client, product)

    response = client.post(reverse("orders:checkout"), _valid_home_delivery_data())

    # Post/Redirect/Get (code-review-and-quality, 2026-07-25): a page
    # refresh on a directly-rendered response would resubmit the form and
    # create a duplicate order. Task 17d: the redirect target is now
    # Paystack's hosted checkout, not our own confirmation page directly
    # -- confirm_order_payment (webhook/callback) is what moves the order
    # to CONFIRMED once payment actually completes.
    order = Order.objects.get()
    assert response.status_code == 302
    assert response.url == _FAKE_AUTHORIZATION_URL
    assert order.status == Order.Status.PENDING
    assert order.customer is None
    assert order.items.count() == 1
    mock_initialize.assert_called_once()
    assert mock_initialize.call_args.kwargs["reference"] == order.payment_reference
    assert mock_initialize.call_args.kwargs["amount_pesewas"] == int(order.total * 100)


@pytest.mark.django_db
@patch("apps.orders.views.initialize_transaction")
def test_submitting_valid_checkout_as_a_logged_in_user_links_the_order_to_the_account(
    mock_initialize, client
):
    mock_initialize.return_value = {"authorization_url": _FAKE_AUTHORIZATION_URL}
    user = User.objects.create_user(username="+233551234568", password="Passw0rd!")
    client.force_login(user)
    product = _make_product()
    _add_to_cart(client, product)

    client.post(reverse("orders:checkout"), _valid_home_delivery_data())

    order = Order.objects.get()
    assert order.customer_id == user.pk


@pytest.mark.django_db
@patch("apps.orders.views.initialize_transaction")
def test_checkout_uses_a_synthetic_email_for_paystack_when_order_email_is_blank(
    mock_initialize, client
):
    # Paystack requires an email on every transaction; Order.email is
    # optional (a guest may leave it blank) -- the synthetic fallback
    # mirrors pay_registration_fee's own established workaround and must
    # never be persisted onto Order.email itself.
    mock_initialize.return_value = {"authorization_url": _FAKE_AUTHORIZATION_URL}
    product = _make_product()
    _add_to_cart(client, product)

    client.post(reverse("orders:checkout"), _valid_home_delivery_data(email=""))

    order = Order.objects.get()
    assert order.email == ""
    assert (
        mock_initialize.call_args.kwargs["email"]
        == f"{order.phone_number}@bancostore.test"
    )


@pytest.mark.django_db
@patch("apps.orders.views.initialize_transaction")
def test_checkout_shows_an_error_if_paystack_initialize_fails(mock_initialize, client):
    from apps.distributors.paystack import PaystackError

    mock_initialize.side_effect = PaystackError("network error")
    product = _make_product()
    _add_to_cart(client, product)

    response = client.post(reverse("orders:checkout"), _valid_home_delivery_data())

    assert response.status_code == 200
    assert Order.objects.exists()  # already created before the Paystack call
    assert "trouble reaching our payment provider" in response.content.decode()


@pytest.mark.django_db
@patch("apps.orders.views.initialize_transaction")
def test_checkout_shows_an_error_if_paystack_returns_no_authorization_url(
    mock_initialize, client
):
    # CodeRabbit (PR #27): every other Paystack response read in this
    # codebase uses .get() so a malformed/unexpected shape logs and
    # returns instead of raising -- data["authorization_url"] was the
    # one bracket-indexed read, turning a missing key into an unhandled
    # 500 after the order already existed.
    mock_initialize.return_value = {}
    product = _make_product()
    _add_to_cart(client, product)

    response = client.post(reverse("orders:checkout"), _valid_home_delivery_data())

    assert response.status_code == 200
    assert Order.objects.exists()
    assert "trouble reaching our payment provider" in response.content.decode()


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
@patch("apps.orders.views.initialize_transaction")
def test_pickup_checkout_does_not_require_address_fields(mock_initialize, client):
    mock_initialize.return_value = {"authorization_url": _FAKE_AUTHORIZATION_URL}
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
@patch("apps.orders.views.initialize_transaction")
def test_pickup_discards_delivery_zone_address_and_area_even_if_submitted(
    mock_initialize, client
):
    # code-review-and-quality (2026-07-25): Alpine only hides the delivery-
    # details fields via x-show when pickup is selected -- it doesn't
    # clear their values, so a user who filled in home-delivery fields
    # before switching to pickup would still submit them. CheckoutForm
    # .clean() must discard them regardless, matching Order's own
    # CheckConstraint (pickup implies blank) -- unproven by any existing
    # test until now, since every other pickup test already posts blank
    # values.
    mock_initialize.return_value = {"authorization_url": _FAKE_AUTHORIZATION_URL}
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


@pytest.mark.django_db
def test_checkout_shows_no_saved_addresses_section_for_a_guest(client):
    product = _make_product()
    _add_to_cart(client, product)

    response = client.get(reverse("orders:checkout"))

    assert response.context["saved_addresses"] == []


@pytest.mark.django_db
def test_checkout_lists_a_logged_in_users_saved_addresses(client):
    from apps.accounts.models import Address

    user = User.objects.create_user(username="ama@example.test", password="pw")
    Address.objects.create(
        user=user, label="Home", address="1 Home St", delivery_zone="kumasi"
    )
    Address.objects.create(
        user=user, label="Office", address="2 Office Ave", delivery_zone="accra"
    )
    client.force_login(user)
    product = _make_product()
    _add_to_cart(client, product)

    response = client.get(reverse("orders:checkout"))

    addresses = response.context["saved_addresses"]
    assert {a.label for a in addresses} == {"Home", "Office"}


@pytest.mark.django_db
def test_checkout_prefills_the_default_saved_address(client):
    from apps.accounts.models import Address

    user = User.objects.create_user(username="ama@example.test", password="pw")
    Address.objects.create(
        user=user,
        label="Old",
        address="Not This One",
        delivery_zone="accra",
        is_default=False,
    )
    Address.objects.create(
        user=user,
        label="Default Home",
        address="9 Default Rd",
        area="Cantonments",
        delivery_zone="kumasi",
        is_default=True,
    )
    client.force_login(user)
    product = _make_product()
    _add_to_cart(client, product)

    response = client.get(reverse("orders:checkout"))

    initial = response.context["form"].initial
    assert initial["address"] == "9 Default Rd"
    assert initial["area"] == "Cantonments"
    assert initial["delivery_zone"] == "kumasi"


@pytest.mark.django_db
def test_checkout_does_not_prefill_an_address_when_no_default_is_set(client):
    from apps.accounts.models import Address

    user = User.objects.create_user(username="ama@example.test", password="pw")
    Address.objects.create(
        user=user, address="Not Default", delivery_zone="accra", is_default=False
    )
    client.force_login(user)
    product = _make_product()
    _add_to_cart(client, product)

    response = client.get(reverse("orders:checkout"))

    assert "address" not in response.context["form"].initial


@pytest.mark.django_db
@patch("apps.orders.views.initialize_transaction")
def test_selecting_a_saved_address_at_checkout_snapshots_correctly_onto_the_order(
    mock_initialize, client
):
    """Selecting a saved address is purely a client-side convenience that
    fills the same visible form fields a manual entry would -- the POST
    payload and Order creation path are identical either way, so this
    proves the snapshot-at-creation-time behavior (Task 17a) is unaffected
    by Task 40b, not a new code path."""
    from apps.accounts.models import Address

    mock_initialize.return_value = {"authorization_url": _FAKE_AUTHORIZATION_URL}
    user = User.objects.create_user(username="ama@example.test", password="pw")
    saved = Address.objects.create(
        user=user,
        label="Home",
        address="9 Default Rd",
        area="Cantonments",
        delivery_zone="kumasi",
        is_default=True,
    )
    client.force_login(user)
    product = _make_product()
    _add_to_cart(client, product)

    client.post(
        reverse("orders:checkout"),
        _valid_home_delivery_data(
            delivery_zone="kumasi", address="9 Default Rd", area="Cantonments"
        ),
    )

    order = Order.objects.get()
    assert order.address == "9 Default Rd"
    assert order.area == "Cantonments"
    assert order.delivery_zone == "kumasi"

    # Editing the saved address afterward must never change the
    # already-placed order's own stored, snapshotted fields.
    saved.address = "A Totally Different Street"
    saved.save()
    order.refresh_from_db()
    assert order.address == "9 Default Rd"
