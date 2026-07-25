from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group

import pytest

from apps.accounts.models import CustomerProfile
from apps.catalog.models import Category, Product
from apps.distributors.models import Distributor
from apps.orders.cart import CartLine
from apps.orders.models import Order, OrderItem
from apps.orders.services import (
    calculate_delivery_fee,
    create_pending_order,
    prefill_contact_info,
)

User = get_user_model()
_phone_seq = count(1)


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


def _make_distributor():
    phone = f"+233244{next(_phone_seq):06d}"
    user = User.objects.create_user(
        username=phone, password="Passw0rd!", email="d@example.test"
    )
    group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(group)
    return Distributor.objects.create(
        user=user, phone_number=phone, full_name="Ama Distributor"
    )


def _make_customer():
    phone = f"+233555{next(_phone_seq):06d}"
    user = User.objects.create_user(
        username=phone, password="Passw0rd!", email="c@example.test"
    )
    CustomerProfile.objects.create(
        user=user, full_name="Kofi Customer", phone_number=phone
    )
    return user


# --- calculate_delivery_fee -------------------------------------------------


@pytest.mark.django_db
def test_pickup_is_always_free_regardless_of_zone_or_subtotal():
    fee = calculate_delivery_fee(Order.DeliveryMethod.PICKUP, "", Decimal("10.00"))
    assert fee == Decimal("0")


@pytest.mark.django_db
def test_kumasi_zone_fee_matches_the_configured_rate():
    fee = calculate_delivery_fee(
        Order.DeliveryMethod.HOME_DELIVERY, Order.DeliveryZone.KUMASI, Decimal("10.00")
    )
    assert fee == Decimal("20")


@pytest.mark.django_db
def test_accra_zone_fee_matches_the_configured_rate():
    fee = calculate_delivery_fee(
        Order.DeliveryMethod.HOME_DELIVERY, Order.DeliveryZone.ACCRA, Decimal("10.00")
    )
    assert fee == Decimal("50")


@pytest.mark.django_db
def test_other_regions_zone_fee_matches_the_configured_rate():
    fee = calculate_delivery_fee(
        Order.DeliveryMethod.HOME_DELIVERY,
        Order.DeliveryZone.OTHER_REGIONS,
        Decimal("10.00"),
    )
    assert fee == Decimal("70")


@pytest.mark.django_db
def test_delivery_is_free_above_the_free_delivery_threshold():
    fee = calculate_delivery_fee(
        Order.DeliveryMethod.HOME_DELIVERY,
        Order.DeliveryZone.OTHER_REGIONS,
        Decimal("500"),
    )
    assert fee == Decimal("0")


@pytest.mark.django_db
def test_delivery_is_charged_just_below_the_free_delivery_threshold():
    fee = calculate_delivery_fee(
        Order.DeliveryMethod.HOME_DELIVERY, Order.DeliveryZone.ACCRA, Decimal("499.99")
    )
    assert fee == Decimal("50")


@pytest.mark.django_db
def test_unknown_zone_raises_instead_of_silently_returning_zero():
    with pytest.raises(ValueError):
        calculate_delivery_fee(
            Order.DeliveryMethod.HOME_DELIVERY, "atlantis", Decimal("10.00")
        )


# --- prefill_contact_info ----------------------------------------------------


@pytest.mark.django_db
def test_anonymous_user_gets_no_prefill():
    assert prefill_contact_info(AnonymousUser()) == {}


@pytest.mark.django_db
def test_distributor_is_prefilled_from_their_own_profile():
    distributor = _make_distributor()

    prefill = prefill_contact_info(distributor.user)

    assert prefill == {
        "full_name": "Ama Distributor",
        "phone_number": distributor.phone_number,
        "email": "d@example.test",
    }


@pytest.mark.django_db
def test_customer_is_prefilled_from_their_own_profile():
    user = _make_customer()

    prefill = prefill_contact_info(user)

    assert prefill["full_name"] == "Kofi Customer"
    assert prefill["email"] == "c@example.test"


@pytest.mark.django_db
def test_a_logged_in_user_with_neither_profile_gets_no_prefill():
    user = User.objects.create_user(username="staffer", password="Passw0rd!")

    assert prefill_contact_info(user) == {}


# --- create_pending_order -----------------------------------------------------


def _cart_items(*products_and_quantities):
    return [
        CartLine(
            product=product,
            quantity=quantity,
            line_total=product.price * quantity,
        )
        for product, quantity in products_and_quantities
    ]


def _home_delivery_form_data(**overrides):
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
def test_creates_a_pending_order_for_a_guest():
    product = _make_product(price=Decimal("450.00"), pv_value=60)
    order = create_pending_order(
        user=AnonymousUser(),
        cart_items=_cart_items((product, 1)),
        form_data=_home_delivery_form_data(),
    )

    assert order.status == Order.Status.PENDING
    assert order.customer is None
    assert order.full_name == "Guest Shopper"


@pytest.mark.django_db
def test_creates_a_pending_order_for_a_logged_in_customer():
    user = _make_customer()
    product = _make_product()

    order = create_pending_order(
        user=user,
        cart_items=_cart_items((product, 1)),
        form_data=_home_delivery_form_data(),
    )

    assert order.customer_id == user.pk


@pytest.mark.django_db
def test_order_totals_are_consistent_with_its_own_order_items():
    product_a = _make_product(name="A", price=Decimal("100.00"))
    product_b = _make_product(name="B", price=Decimal("50.00"))

    order = create_pending_order(
        user=AnonymousUser(),
        cart_items=_cart_items((product_a, 2), (product_b, 3)),
        form_data=_home_delivery_form_data(),
    )

    line_total_sum = sum(item.unit_price * item.quantity for item in order.items.all())
    assert order.subtotal == line_total_sum == Decimal("350.00")
    assert order.delivery_fee == Decimal("50")  # Accra, below free threshold
    assert order.total == Decimal("400.00")


@pytest.mark.django_db
def test_pickup_order_has_zero_delivery_fee_and_blank_address_fields():
    product = _make_product(price=Decimal("100.00"))

    order = create_pending_order(
        user=AnonymousUser(),
        cart_items=_cart_items((product, 1)),
        form_data=_home_delivery_form_data(
            delivery_method=Order.DeliveryMethod.PICKUP,
            delivery_zone="",
            address="",
            area="",
        ),
    )

    assert order.delivery_fee == Decimal("0")
    assert order.total == order.subtotal
    assert order.address == ""


@pytest.mark.django_db
def test_order_items_snapshot_price_and_pv_not_a_live_reference():
    product = _make_product(price=Decimal("450.00"), pv_value=60)

    order = create_pending_order(
        user=AnonymousUser(),
        cart_items=_cart_items((product, 1)),
        form_data=_home_delivery_form_data(),
    )

    product.price = Decimal("999.00")
    product.pv_value = 1
    product.save()

    item = OrderItem.objects.get(order=order)
    assert item.unit_price == Decimal("450.00")
    assert item.unit_pv == 60


@pytest.mark.django_db
def test_order_creation_does_not_decrement_stock():
    product = _make_product(stock=5)

    create_pending_order(
        user=AnonymousUser(),
        cart_items=_cart_items((product, 2)),
        form_data=_home_delivery_form_data(),
    )

    product.refresh_from_db()
    assert product.stock == 5


@pytest.mark.django_db
def test_order_creation_does_not_credit_pv():
    product = _make_product(pv_value=60)

    order = create_pending_order(
        user=AnonymousUser(),
        cart_items=_cart_items((product, 1)),
        form_data=_home_delivery_form_data(),
    )

    assert order.pv_earned == 0


@pytest.mark.django_db
def test_payment_reference_is_unique_across_orders():
    product = _make_product()

    order_one = create_pending_order(
        user=AnonymousUser(),
        cart_items=_cart_items((product, 1)),
        form_data=_home_delivery_form_data(),
    )
    order_two = create_pending_order(
        user=AnonymousUser(),
        cart_items=_cart_items((product, 1)),
        form_data=_home_delivery_form_data(),
    )

    assert order_one.payment_reference != order_two.payment_reference
