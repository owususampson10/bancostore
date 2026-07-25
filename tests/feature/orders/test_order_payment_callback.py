from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product
from apps.orders.models import Order


def _make_order(**overrides):
    defaults = {
        "full_name": "Ama Mensah",
        "phone_number": "+233241234567",
        "email": "ama@example.test",
        "delivery_method": Order.DeliveryMethod.PICKUP,
        "subtotal": Decimal("450.00"),
        "delivery_fee": Decimal("0"),
        "total": Decimal("450.00"),
        "payment_reference": "order-callback-test-1",
    }
    defaults.update(overrides)
    return Order.objects.create(**defaults)


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
@patch("apps.orders.views.confirm_order_payment")
def test_callback_with_a_reference_reverifies_server_side_and_redirects_to_confirmation(
    mock_confirm, client
):
    order = _make_order()

    response = client.get(
        reverse("orders:order_payment_callback"),
        {"reference": order.payment_reference},
    )

    # Never trusts the callback's own query-string claims about payment
    # status -- confirm_order_payment always re-verifies with Paystack
    # itself, exactly once, before anything is shown to the customer.
    mock_confirm.assert_called_once_with(order.payment_reference)
    assert response.status_code == 302
    assert response.url == reverse(
        "orders:order_confirmation", args=[order.payment_reference]
    )


@pytest.mark.django_db
@patch("apps.orders.views.confirm_order_payment")
def test_callback_with_no_reference_redirects_to_cart_without_calling_confirm(
    mock_confirm, client
):
    response = client.get(reverse("orders:order_payment_callback"))

    mock_confirm.assert_not_called()
    assert response.status_code == 302
    assert response.url == reverse("orders:cart")


@pytest.mark.django_db
@patch("apps.orders.views.confirm_order_payment")
def test_callback_with_an_unknown_reference_redirects_to_cart_without_calling_confirm(
    mock_confirm, client
):
    # CodeRabbit (PR #27): a reference that matches no Order must not be
    # handed to confirm_order_payment (which would just log-and-return),
    # nor reversed into a URL that assumes a real order exists.
    response = client.get(
        reverse("orders:order_payment_callback"), {"reference": "order-does-not-exist"}
    )

    mock_confirm.assert_not_called()
    assert response.status_code == 302
    assert response.url == reverse("orders:cart")


@pytest.mark.django_db
def test_callback_with_a_slash_in_the_reference_does_not_500(client):
    # CodeRabbit (PR #27), reproduced via manage.py shell before fixing:
    # reverse("orders:order_confirmation", args=[reference]) raises
    # NoReverseMatch for any value containing "/", since the URL pattern
    # uses a <str:...> converter -- an unauthenticated, fully
    # attacker-controlled query-string value reaching reverse()
    # unvalidated turned into an unhandled 500 on a public GET endpoint.
    response = client.get(
        reverse("orders:order_payment_callback"), {"reference": "order-x/y"}
    )

    assert response.status_code == 302
    assert response.url == reverse("orders:cart")


@pytest.mark.django_db
@patch("apps.orders.views.confirm_order_payment")
def test_callback_clears_the_cart_once_the_order_is_actually_confirmed(
    mock_confirm, client
):
    # CodeRabbit (PR #27, Major): the cart was never cleared after a
    # confirmed payment, so the customer could immediately re-order the
    # same items. confirm_order_payment is mocked here as a no-op --
    # this test is about the callback's own post-confirm cart-clearing,
    # not about confirm_order_payment itself (covered exhaustively in
    # tests/unit/orders/test_confirm_order_payment.py) -- so the order is
    # already CONFIRMED before the callback runs, standing in for what a
    # real confirm_order_payment call would have just done.
    product = _make_product()
    client.post(reverse("orders:cart_add", args=[product.pk]))
    assert client.get(reverse("orders:cart")).context["cart_items"]

    order = _make_order(status=Order.Status.CONFIRMED)

    client.get(
        reverse("orders:order_payment_callback"),
        {"reference": order.payment_reference},
    )

    assert client.get(reverse("orders:cart")).context["cart_items"] == []


@pytest.mark.django_db
@patch("apps.orders.views.confirm_order_payment")
def test_callback_does_not_clear_the_cart_when_the_order_stays_pending(
    mock_confirm, client
):
    product = _make_product()
    client.post(reverse("orders:cart_add", args=[product.pk]))

    order = _make_order(status=Order.Status.PENDING)

    client.get(
        reverse("orders:order_payment_callback"),
        {"reference": order.payment_reference},
    )

    assert len(client.get(reverse("orders:cart")).context["cart_items"]) == 1
