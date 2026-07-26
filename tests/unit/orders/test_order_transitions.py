from decimal import Decimal
from itertools import count
from unittest.mock import patch

import pytest

from apps.orders.models import Order
from apps.orders.services import advance_order_status

_phone_seq = count(1)


def _make_order(*, status=Order.Status.CONFIRMED, total=Decimal("450.00")):
    return Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=total,
        delivery_fee=Decimal("0"),
        total=total,
        payment_reference=f"order-test-ref-{next(_phone_seq)}",
        status=status,
    )


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_confirmed_to_processing_succeeds(mock_sms, mock_mail):
    order = _make_order(status=Order.Status.CONFIRMED)

    advance_order_status(order.pk, Order.Status.PROCESSING)

    order.refresh_from_db()
    assert order.status == Order.Status.PROCESSING


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_processing_to_dispatched_succeeds(mock_sms, mock_mail):
    order = _make_order(status=Order.Status.PROCESSING)

    advance_order_status(order.pk, Order.Status.DISPATCHED)

    order.refresh_from_db()
    assert order.status == Order.Status.DISPATCHED


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_dispatched_to_delivered_succeeds(mock_sms, mock_mail):
    order = _make_order(status=Order.Status.DISPATCHED)

    advance_order_status(order.pk, Order.Status.DELIVERED)

    order.refresh_from_db()
    assert order.status == Order.Status.DELIVERED


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_skipping_a_stage_is_rejected(mock_sms, mock_mail):
    """CONFIRMED -> DISPATCHED skips PROCESSING -- not in the 18a
    transition graph."""
    order = _make_order(status=Order.Status.CONFIRMED)

    with pytest.raises(RuntimeError):
        advance_order_status(order.pk, Order.Status.DISPATCHED)

    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED


@pytest.mark.django_db
def test_to_status_outside_the_allowed_set_is_rejected():
    """CANCELLED/REFUNDED belong to Task 18b's cancel_or_refund_order --
    this function must reject them outright, before touching the DB."""
    order = _make_order(status=Order.Status.CONFIRMED)

    with pytest.raises(ValueError):
        advance_order_status(order.pk, Order.Status.CANCELLED)

    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_a_duplicate_transition_to_the_same_status_is_a_no_op(mock_sms, mock_mail):
    order = _make_order(status=Order.Status.PROCESSING)

    advance_order_status(order.pk, Order.Status.PROCESSING)

    order.refresh_from_db()
    assert order.status == Order.Status.PROCESSING
    mock_sms.assert_not_called()


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_notification_fires_on_a_real_transition(mock_sms, mock_mail):
    order = _make_order(status=Order.Status.CONFIRMED)

    advance_order_status(order.pk, Order.Status.PROCESSING)

    mock_sms.assert_called_once()
    mock_mail.assert_called_once()
    assert "Processing" in mock_sms.call_args[0][1]


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_tracking_note_persists_alongside_a_status_update(mock_sms, mock_mail):
    order = _make_order(status=Order.Status.PROCESSING)

    advance_order_status(
        order.pk, Order.Status.DISPATCHED, tracking_note="Handed to Speedaf courier."
    )

    order.refresh_from_db()
    assert order.status == Order.Status.DISPATCHED
    assert order.tracking_note == "Handed to Speedaf courier."


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_blank_tracking_note_does_not_clear_an_existing_one(mock_sms, mock_mail):
    order = _make_order(status=Order.Status.PROCESSING)
    order.tracking_note = "Already noted at Processing."
    order.save(update_fields=["tracking_note"])

    advance_order_status(order.pk, Order.Status.DISPATCHED)

    order.refresh_from_db()
    assert order.tracking_note == "Already noted at Processing."


@pytest.mark.django_db
def test_a_missing_order_is_a_safe_no_op():
    advance_order_status(999999, Order.Status.PROCESSING)  # must not raise
