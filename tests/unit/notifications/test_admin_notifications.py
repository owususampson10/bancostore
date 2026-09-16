"""Task 61c. The admin portal's notification bell.

Stated limit, up front: a bell only reaches someone already logged in, so
it does NOT solve the problem 61a/61b solve (nobody finds out until
somebody looks). It was built because all three were asked for, not as a
substitute for the email and SMS.
"""

from decimal import Decimal

import pytest

from apps.notifications.models import AdminNotification
from apps.notifications.services import send_admin_notification
from apps.orders.models import Order


def _make_order(**overrides):
    defaults = {
        "full_name": "Kofi Mensah",
        "phone_number": "+233241234567",
        "email": "kofi@example.com",
        "delivery_method": Order.DeliveryMethod.PICKUP,
        "subtotal": Decimal("350.00"),
        "delivery_fee": Decimal("0.00"),
        "discount_amount": Decimal("0.00"),
        "total": Decimal("350.00"),
        "status": Order.Status.CONFIRMED,
    }
    defaults.update(overrides)
    return Order.objects.create(**defaults)


@pytest.mark.django_db
def test_sending_creates_a_row():
    order = _make_order()

    send_admin_notification(
        AdminNotification.EventType.NEW_ORDER, "New order", order=order
    )

    notification = AdminNotification.objects.get()
    assert notification.message == "New order"
    assert notification.order_id == order.pk
    assert notification.is_read is False


@pytest.mark.django_db
def test_the_order_link_survives_the_order_being_deleted():
    """order is SET_NULL, not CASCADE: an admin reading their bell history
    should still see that an order arrived, even if the row was later
    removed. Losing the audit line entirely would be worse than losing
    the link."""
    order = _make_order()
    send_admin_notification(
        AdminNotification.EventType.NEW_ORDER, "New order", order=order
    )

    order.items.all().delete()
    order.delete()

    notification = AdminNotification.objects.get()
    assert notification.order_id is None
    assert notification.message == "New order"


@pytest.mark.django_db
def test_notifications_come_back_newest_first():
    for i in range(3):
        send_admin_notification(
            AdminNotification.EventType.NEW_ORDER, f"Order {i}", order=None
        )

    messages = list(AdminNotification.objects.values_list("message", flat=True))

    assert messages == ["Order 2", "Order 1", "Order 0"]


@pytest.mark.django_db
def test_unread_count_ignores_read_rows():
    send_admin_notification(AdminNotification.EventType.NEW_ORDER, "one")
    second = send_admin_notification(AdminNotification.EventType.NEW_ORDER, "two")
    second.is_read = True
    second.save(update_fields=["is_read"])

    assert AdminNotification.unread_count() == 1


@pytest.mark.django_db
def test_every_event_type_has_an_icon():
    """Mirrors the distributor Notification model's own guarantee -- a new
    event type with no icon entry would render a blank circle in the bell
    rather than failing loudly anywhere."""
    for event_type in AdminNotification.EventType:
        notification = AdminNotification(event_type=event_type, message="x")
        icon, classes = notification.icon
        assert icon
        assert classes


@pytest.mark.django_db
def test_a_notification_failure_never_breaks_the_caller():
    """Called from the order-confirmation path, after money has already
    moved. A bell row failing to write must never look like the order
    failed -- the same contract every other notification site here has."""
    from unittest.mock import patch

    with patch.object(
        AdminNotification.objects, "create", side_effect=RuntimeError("db down")
    ):
        assert (
            send_admin_notification(AdminNotification.EventType.NEW_ORDER, "boom")
            is None
        )


# --- Task 61c: the bell's views and badge --------------------------------


@pytest.mark.django_db
def test_the_badge_costs_nothing_for_a_non_staff_visitor(rf):
    """This context processor runs on EVERY request in the project,
    storefront included. An unconditional count would be a real cost at
    this project's stated scale for a number no shopper can see."""
    from django.contrib.auth.models import AnonymousUser

    from apps.admin_portal.context_processors import admin_notification_badge

    request = rf.get("/")
    request.user = AnonymousUser()

    assert admin_notification_badge(request) == {}


@pytest.mark.django_db
def test_the_badge_counts_unread_for_a_staff_user(rf):
    from django.contrib.auth import get_user_model

    from apps.admin_portal.context_processors import admin_notification_badge

    send_admin_notification(AdminNotification.EventType.NEW_ORDER, "one")
    request = rf.get("/")
    request.user = get_user_model().objects.create_user(
        username="badge-admin", password="Passw0rd!", is_staff=True
    )

    assert admin_notification_badge(request) == {"admin_unread_count": 1}


@pytest.mark.django_db
def test_marking_all_read_requires_a_post(client):
    """A GET must never clear an admin's whole bell -- a prefetch, a
    crawler or a browser preloading the link would otherwise do it."""
    from django.contrib.auth import get_user_model
    from django.urls import reverse

    user = get_user_model().objects.create_user(
        username="post-admin", password="Passw0rd!", is_staff=True
    )
    client.force_login(user)
    send_admin_notification(AdminNotification.EventType.NEW_ORDER, "one")

    response = client.get(reverse("admin_portal:admin_notification_mark_all_read"))

    assert response.status_code == 405
    assert AdminNotification.unread_count() == 1


@pytest.mark.django_db
def test_marking_all_read_clears_the_count(client):
    from django.contrib.auth import get_user_model
    from django.urls import reverse

    user = get_user_model().objects.create_user(
        username="clear-admin", password="Passw0rd!", is_staff=True
    )
    client.force_login(user)
    send_admin_notification(AdminNotification.EventType.NEW_ORDER, "one")

    response = client.post(reverse("admin_portal:admin_notification_mark_all_read"))

    assert response.status_code == 200
    assert AdminNotification.unread_count() == 0
