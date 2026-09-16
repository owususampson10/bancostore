"""Task 61a. The admin is told when an order is paid for.

Before this, a confirmed order notified the customer and nobody else --
the admin found out by logging in and looking at a dashboard card. If
nobody logged in, paid orders sat unseen while the customer waited.
"""

from decimal import Decimal
from unittest.mock import patch

from django.core import mail

import pytest
from constance import config

from apps.catalog.models import Category, Product
from apps.orders.admin_alerts import send_admin_order_alert
from apps.orders.models import Order, OrderItem


def _make_order(**overrides):
    defaults = {
        "full_name": "Kofi Mensah",
        "phone_number": "+233241234567",
        "email": "kofi@example.com",
        "delivery_method": Order.DeliveryMethod.HOME_DELIVERY,
        "delivery_zone": Order.DeliveryZone.ACCRA,
        "address": "12 High St",
        "area": "Osu",
        "landmark": "",
        "subtotal": Decimal("350.00"),
        "delivery_fee": Decimal("50.00"),
        "discount_amount": Decimal("0.00"),
        "total": Decimal("400.00"),
        "status": Order.Status.CONFIRMED,
    }
    defaults.update(overrides)
    return Order.objects.create(**defaults)


def _add_item(order, name="Emerald Aura Watch", quantity=1, unit_price="350.00"):
    category, _ = Category.objects.get_or_create(name="Watches", slug="watches")
    product = Product.objects.create(
        name=name,
        category=category,
        price=Decimal(unit_price),
        pv_value=10,
        stock=5,
        is_active=True,
    )
    return OrderItem.objects.create(
        order=order,
        product=product,
        product_name=name,
        quantity=quantity,
        unit_price=Decimal(unit_price),
        unit_pv=10,
    )


@pytest.mark.django_db
def test_an_email_goes_to_the_configured_admin_address(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.ADMIN_ORDER_ALERT_EMAIL = "ops@bancostore.test"
    order = _make_order()
    _add_item(order)

    send_admin_order_alert(order)

    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == ["ops@bancostore.test"]


@pytest.mark.django_db
def test_the_alert_carries_what_the_admin_needs_to_act(settings):
    """An alert that only says "you have an order" makes the admin log in
    to learn anything at all, which is the problem this solves."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.ADMIN_ORDER_ALERT_EMAIL = "ops@bancostore.test"
    order = _make_order()
    _add_item(order, name="Emerald Aura Watch", quantity=2, unit_price="175.00")

    send_admin_order_alert(order)

    body = mail.outbox[0].body
    assert order.payment_reference in body
    assert "400.00" in body  # the total
    assert "Kofi Mensah" in body  # who to contact
    assert "+233241234567" in body  # how to contact them
    assert "Emerald Aura Watch" in body  # what to pack
    assert "12 High St" in body  # where it goes


@pytest.mark.django_db
def test_a_blank_setting_disables_the_alert_entirely(settings):
    """Blank means disabled, the same convention as
    COMPLIANCE_ALERT_EMAIL and MNOTIFY_API_KEY. A store that has not
    configured an address must not have send_mail called at all."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.ADMIN_ORDER_ALERT_EMAIL = ""
    order = _make_order()
    _add_item(order)

    send_admin_order_alert(order)

    assert mail.outbox == []


@pytest.mark.django_db
def test_a_whitespace_only_setting_counts_as_blank(settings):
    """An address of "   " is not a configured address. Without this the
    send would go out to a whitespace recipient and fail obscurely."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.ADMIN_ORDER_ALERT_EMAIL = "   "
    order = _make_order()
    _add_item(order)

    send_admin_order_alert(order)

    assert mail.outbox == []


@pytest.mark.django_db
def test_a_send_failure_never_propagates(settings):
    """This is called from confirm_order_payment AFTER money, stock and PV
    have committed, inside retry_on_lock_contention's _attempt. An
    exception escaping here would make a delivered order look like a
    rolled-back one, or re-run an already-committed transaction -- the
    exact failure CodeRabbit caught in _send_confirmation_notifications
    on PR #91."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.ADMIN_ORDER_ALERT_EMAIL = "ops@bancostore.test"
    order = _make_order()
    _add_item(order)

    with patch(
        "apps.orders.admin_alerts.send_mail", side_effect=RuntimeError("smtp down")
    ):
        send_admin_order_alert(order)  # must not raise


@pytest.mark.django_db
def test_a_broken_context_never_propagates_either(settings):
    """The context is built inside the guard, not above it -- same
    reasoning as the send failure above."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.ADMIN_ORDER_ALERT_EMAIL = "ops@bancostore.test"
    order = _make_order()
    _add_item(order)

    with patch(
        "apps.orders.admin_alerts.build_receipt_context",
        side_effect=RuntimeError("context blew up"),
    ):
        send_admin_order_alert(order)  # must not raise

    assert mail.outbox == []


@pytest.mark.django_db
def test_a_live_admin_edit_reaches_the_next_alert(settings):
    """Per-send-site proof that the admin-editable row is what renders,
    matching the convention Task 48 set for every other send site."""
    from apps.notifications.models import NotificationTemplate

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.ADMIN_ORDER_ALERT_EMAIL = "ops@bancostore.test"
    order = _make_order()
    _add_item(order)
    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.ADMIN_NEW_ORDER_EMAIL
    ).update(subject="Edited subject", body="Order {{reference}} needs packing.")

    send_admin_order_alert(order)

    assert mail.outbox[0].subject == "Edited subject"
    assert mail.outbox[0].body == f"Order {order.payment_reference} needs packing."


@pytest.mark.django_db
def test_confirm_order_payment_actually_fires_the_admin_alert():
    """The wiring, not just the function. A perfectly good alert nobody
    calls is the bug this task exists to fix."""
    from apps.orders import services

    order = _make_order(status=Order.Status.PENDING)
    _add_item(order)

    with (
        patch.object(services, "verify_transaction") as mock_verify,
        patch.object(services, "send_admin_order_alert") as mock_alert,
        patch.object(services, "_send_confirmation_notifications"),
    ):
        mock_verify.return_value = {
            "status": "success",
            "amount": int(order.total * 100),
            "currency": "GHS",
        }
        services.confirm_order_payment(order.payment_reference)

    mock_alert.assert_called_once()
    assert mock_alert.call_args.args[0].pk == order.pk


@pytest.mark.django_db
def test_an_admin_alert_failure_never_breaks_a_confirmed_order():
    """confirm_order_payment runs the alert AFTER money/stock/PV commit,
    inside retry_on_lock_contention's _attempt. The order must stay
    confirmed even if alerting blows up entirely."""
    from apps.orders import services

    order = _make_order(status=Order.Status.PENDING)
    _add_item(order)

    with (
        patch.object(services, "verify_transaction") as mock_verify,
        patch.object(
            services,
            "send_admin_order_alert",
            side_effect=RuntimeError("alert exploded"),
        ),
        patch.object(services, "_send_confirmation_notifications"),
    ):
        mock_verify.return_value = {
            "status": "success",
            "amount": int(order.total * 100),
            "currency": "GHS",
        }
        services.confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED


# --- Task 61b: the SMS channel -------------------------------------------


@pytest.mark.django_db
def test_an_sms_goes_to_the_configured_admin_number():
    config.ADMIN_ORDER_ALERT_SMS_NUMBER = "+233201112222"
    order = _make_order()
    _add_item(order)

    with patch("apps.orders.admin_alerts.send_sms") as mock_sms:
        send_admin_order_alert(order)

    mock_sms.assert_called_once()
    assert mock_sms.call_args.args[0] == "+233201112222"


@pytest.mark.django_db
def test_the_sms_stays_short_enough_not_to_multiply_cost():
    """mNotify bills per 160 characters and this fires on every confirmed
    order, so an item list here would multiply the store's whole
    messaging bill. One segment is the budget."""
    config.ADMIN_ORDER_ALERT_SMS_NUMBER = "+233201112222"
    order = _make_order()
    _add_item(order, name="A Very Long Product Name That Goes On And On", quantity=3)

    with patch("apps.orders.admin_alerts.send_sms") as mock_sms:
        send_admin_order_alert(order)

    body = mock_sms.call_args.args[1]
    assert len(body) <= 160
    assert order.payment_reference in body
    assert "400.00" in body


@pytest.mark.django_db
def test_a_blank_sms_number_disables_only_the_sms(settings):
    """The two channels are independent settings. Configuring email but
    not SMS must send the email and no SMS."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.ADMIN_ORDER_ALERT_EMAIL = "ops@bancostore.test"
    config.ADMIN_ORDER_ALERT_SMS_NUMBER = ""
    order = _make_order()
    _add_item(order)

    with patch("apps.orders.admin_alerts.send_sms") as mock_sms:
        send_admin_order_alert(order)

    mock_sms.assert_not_called()
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_an_sms_failure_does_not_skip_the_email(settings):
    """Each channel independently guarded -- the convention every other
    multi-channel send site in this codebase already follows."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.ADMIN_ORDER_ALERT_EMAIL = "ops@bancostore.test"
    config.ADMIN_ORDER_ALERT_SMS_NUMBER = "+233201112222"
    order = _make_order()
    _add_item(order)

    with patch(
        "apps.orders.admin_alerts.send_sms", side_effect=RuntimeError("mnotify down")
    ):
        send_admin_order_alert(order)  # must not raise

    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_an_email_failure_does_not_skip_the_sms(settings):
    """The mirror of the test above -- proving the guards are genuinely
    per-channel and not just one try block around both."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.ADMIN_ORDER_ALERT_EMAIL = "ops@bancostore.test"
    config.ADMIN_ORDER_ALERT_SMS_NUMBER = "+233201112222"
    order = _make_order()
    _add_item(order)

    with (
        patch(
            "apps.orders.admin_alerts.send_mail", side_effect=RuntimeError("smtp down")
        ),
        patch("apps.orders.admin_alerts.send_sms") as mock_sms,
    ):
        send_admin_order_alert(order)

    mock_sms.assert_called_once()


# --- Task 61c: the in-app bell -------------------------------------------


@pytest.mark.django_db
def test_a_confirmed_order_records_a_bell_notification():
    from apps.notifications.models import AdminNotification

    config.ADMIN_ORDER_ALERT_EMAIL = ""
    config.ADMIN_ORDER_ALERT_SMS_NUMBER = ""
    order = _make_order()
    _add_item(order)

    send_admin_order_alert(order)

    notification = AdminNotification.objects.get()
    assert notification.order_id == order.pk
    assert order.payment_reference in notification.message
    assert "Kofi Mensah" in notification.message


@pytest.mark.django_db
def test_the_bell_fires_even_with_both_other_channels_disabled():
    """The bell has no recipient setting -- there is nobody to address,
    so there is nothing to leave misconfigured. A store that has set
    neither an email nor a number still gets the in-app record."""
    from apps.notifications.models import AdminNotification

    config.ADMIN_ORDER_ALERT_EMAIL = ""
    config.ADMIN_ORDER_ALERT_SMS_NUMBER = ""
    order = _make_order()
    _add_item(order)

    send_admin_order_alert(order)

    assert AdminNotification.objects.count() == 1


@pytest.mark.django_db
def test_a_bell_failure_does_not_skip_the_email(settings):
    """Third channel, same independence contract as the first two."""
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    config.ADMIN_ORDER_ALERT_EMAIL = "ops@bancostore.test"
    config.ADMIN_ORDER_ALERT_SMS_NUMBER = ""
    order = _make_order()
    _add_item(order)

    with patch(
        "apps.orders.admin_alerts.send_admin_notification",
        side_effect=RuntimeError("bell broke"),
    ):
        send_admin_order_alert(order)

    assert len(mail.outbox) == 1


# --- CodeRabbit (PR #92) -------------------------------------------------


@pytest.mark.django_db
def test_a_broken_settings_backend_does_not_skip_later_channels(settings):
    """CodeRabbit (PR #92): the constance lookups sat ABOVE their try
    blocks. constance reads through Redis, so an outage there raised
    before the guard and skipped every later channel -- the same class of
    bug as the receipt context on PR #91, which I should have generalised
    at the time."""
    from apps.notifications.models import AdminNotification

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    order = _make_order()
    _add_item(order)

    class _Boom:
        def __getattr__(self, name):
            raise RuntimeError("redis down")

    with patch("apps.orders.admin_alerts.config", _Boom()):
        send_admin_order_alert(order)  # must not raise

    # The bell reads no setting at all, so it must still have fired.
    assert AdminNotification.objects.count() == 1


@pytest.mark.django_db
def test_an_oversized_admin_edited_sms_is_truncated():
    """CodeRabbit (PR #92): the 160-character cap was only ever asserted
    against the DEFAULT wording. An admin edit -- or simply a long
    customer name -- could blow past it and spend several SMS credits per
    order, defeating the entire reason the body was kept short."""
    from apps.notifications.models import NotificationTemplate

    config.ADMIN_ORDER_ALERT_SMS_NUMBER = "+233201112222"
    # 200, not 300: Order.full_name is max_length=255. SQLite ignores
    # varchar limits but MySQL rejects the row outright, so the original
    # 300 passed locally and failed CI -- the very bug class this batch
    # fixed elsewhere, reproduced in its own test. The over-length body
    # comes from the repeated template below, not from one giant name.
    order = _make_order(full_name="A" * 200)
    _add_item(order)
    NotificationTemplate.objects.filter(
        key=NotificationTemplate.Key.ADMIN_NEW_ORDER_SMS
    ).update(body="{{customer_name}} " * 40)

    with patch("apps.orders.admin_alerts.send_sms") as mock_sms:
        send_admin_order_alert(order)

    body = mock_sms.call_args.args[1]
    assert len(body) <= 160
