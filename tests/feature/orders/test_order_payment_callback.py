from decimal import Decimal
from unittest.mock import patch

from django.urls import reverse

import pytest

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
