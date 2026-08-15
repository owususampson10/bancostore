from decimal import Decimal
from itertools import count
from unittest.mock import patch

import pytest

from apps.notifications.models import NotificationTemplate
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
def test_order_status_update_uses_the_live_admin_edited_sms_wording(
    mock_sms, mock_mail
):
    """Task 48c acceptance criteria: editing a template's wording from
    admin_portal changes the next real send."""
    template, _ = NotificationTemplate.objects.get_or_create(
        key=NotificationTemplate.Key.ORDER_STATUS_UPDATE_SMS
    )
    template.body = "Custom: order {{reference}} -> {{status}}!"
    template.save()
    order = _make_order(status=Order.Status.CONFIRMED)

    advance_order_status(order.pk, Order.Status.PROCESSING)

    assert mock_sms.call_args[0][1] == (
        f"Custom: order {order.payment_reference} -> Processing!"
    )


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_order_status_update_uses_the_live_admin_edited_email_wording(
    mock_sms, mock_mail
):
    template, _ = NotificationTemplate.objects.get_or_create(
        key=NotificationTemplate.Key.ORDER_STATUS_UPDATE_EMAIL
    )
    template.subject = "Custom subject: {{status}}"
    template.body = "Custom body: order {{reference}} -> {{status}}!"
    template.save()
    order = _make_order(status=Order.Status.CONFIRMED)

    advance_order_status(order.pk, Order.Status.PROCESSING)

    assert mock_mail.call_args.kwargs["subject"] == "Custom subject: Processing"
    assert mock_mail.call_args.kwargs["message"] == (
        f"Custom body: order {order.payment_reference} -> Processing!"
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "from_status,to_status",
    [
        (Order.Status.CONFIRMED, Order.Status.PROCESSING),
        (Order.Status.PROCESSING, Order.Status.DISPATCHED),
        (Order.Status.DISPATCHED, Order.Status.DELIVERED),
    ],
)
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_both_notification_channels_fire_with_the_new_status_for_every_transition(
    mock_sms, mock_mail, from_status, to_status
):
    """CodeRabbit (PR #30): the single-transition test above only proved
    SMS content once -- _send_order_status_notification itself has no
    per-transition branching (same function, same message shape,
    regardless of which status triggered it, already proven by 18b's own
    suite using the exact same helper), so this parametrization mainly
    guards against a regression in the from/to wiring itself, not a
    distinct code path per stage."""
    order = _make_order(status=from_status)

    advance_order_status(order.pk, to_status)

    mock_sms.assert_called_once()
    mock_mail.assert_called_once()
    order.refresh_from_db()
    expected = order.get_status_display()
    assert expected in mock_sms.call_args[0][1]
    assert expected in mock_mail.call_args.kwargs["message"]


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
def test_whitespace_only_tracking_note_does_not_clear_an_existing_one(
    mock_sms, mock_mail
):
    """CodeRabbit (PR #30): a whitespace-only string is truthy in Python,
    so a naive `if tracking_note:` check would persist "   " as if it
    were a real note, silently overwriting a genuine existing one."""
    order = _make_order(status=Order.Status.PROCESSING)
    order.tracking_note = "Already noted at Processing."
    order.save(update_fields=["tracking_note"])

    advance_order_status(order.pk, Order.Status.DISPATCHED, tracking_note="   ")

    order.refresh_from_db()
    assert order.tracking_note == "Already noted at Processing."


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
