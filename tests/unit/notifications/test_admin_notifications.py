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
def test_marking_all_read_requires_a_post(staff_client):
    """A GET must never clear an admin's whole bell -- a prefetch, a
    crawler or a browser preloading the link would otherwise do it."""
    from django.urls import reverse

    send_admin_notification(AdminNotification.EventType.NEW_ORDER, "one")

    response = staff_client.get(
        reverse("admin_portal:admin_notification_mark_all_read")
    )

    assert response.status_code == 405
    assert AdminNotification.unread_count() == 1


@pytest.mark.django_db
def test_marking_all_read_clears_the_count(staff_client):
    from django.urls import reverse

    send_admin_notification(AdminNotification.EventType.NEW_ORDER, "one")

    response = staff_client.post(
        reverse("admin_portal:admin_notification_mark_all_read")
    )

    assert response.status_code == 200
    assert AdminNotification.unread_count() == 0


# --- CodeRabbit (PR #92) -------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_notification_failure_does_not_poison_an_outer_transaction():
    """CodeRabbit (PR #92): catching a database error inside an open
    atomic block does NOT make it usable again -- Django marks the block
    for rollback and the caller fails on its next query.

    Code review (PR #92) proved the first two versions of this test
    passed with the fix reverted. Mocking create() means no SQL runs, so
    nothing ever marks the block -- and the trigger is whether a real
    statement executed, NOT the exception type (Model.save_base wraps the
    write in mark_for_rollback_on_error, which catches bare Exception).
    This version performs a REAL insert that violates a REAL constraint,
    which fires immediately on both SQLite and MySQL.
    """
    from unittest.mock import patch

    from django.db import transaction

    from apps.orders.models import Order

    existing = AdminNotification.objects.create(
        event_type=AdminNotification.EventType.NEW_ORDER, message="already here"
    )

    def _duplicate_pk_insert(**kwargs):
        return AdminNotification(pk=existing.pk, **kwargs).save(force_insert=True)

    with transaction.atomic():
        with patch.object(
            AdminNotification.objects, "create", side_effect=_duplicate_pk_insert
        ):
            send_admin_notification(
                AdminNotification.EventType.NEW_ORDER, "boom", order=None
            )
        # Without the savepoint this raises TransactionManagementError.
        assert Order.objects.count() >= 0


@pytest.mark.django_db
def test_an_overlong_bell_message_is_truncated_to_the_column_width():
    """Code review (PR #92): the guard _create_and_push already has for
    the distributor path was missing here. SQLite ignores varchar limits,
    so an over-length message only fails on MySQL -- silently dropping
    that order's bell notification in production while every local test
    passed."""
    max_length = AdminNotification._meta.get_field("message").max_length

    notification = send_admin_notification(
        AdminNotification.EventType.NEW_ORDER, "x" * (max_length + 200)
    )

    assert len(notification.message) == max_length


@pytest.mark.django_db
def test_marking_all_read_reports_the_real_count_not_a_hardcoded_zero(staff_client):
    """CodeRabbit (PR #92): the view returned unread_count=0 literally. A
    notification arriving between the bulk update and the render would be
    invisible -- the WebSocket sets the badge to 1, and this response then
    stomps it back to 0.

    Patching unread_count proves the response REPORTS the live value
    rather than asserting a constant that happens to match."""
    from unittest.mock import patch

    from django.urls import reverse

    send_admin_notification(AdminNotification.EventType.NEW_ORDER, "first")

    with patch.object(AdminNotification, "unread_count", return_value=7):
        response = staff_client.post(
            reverse("admin_portal:admin_notification_mark_all_read")
        )

    assert response.status_code == 200
    assert b'data-unread-count="7"' in response.content


# --- Code review (PR #92): the authorization hole -------------------------


@pytest.mark.django_db
def test_a_non_staff_user_cannot_read_the_admin_bell(client):
    """The critical finding. @login_required alone let ANY logged-in
    customer or distributor GET this endpoint and read the last 10 admin
    notifications -- other customers' names, order references and GHS
    totals. Every other admin_portal view has the is_admin_portal_staff
    body check; these two did not."""
    from django.contrib.auth import get_user_model
    from django.urls import reverse

    user = get_user_model().objects.create_user(
        username="nosy-customer", password="Passw0rd!", is_staff=False
    )
    client.force_login(user)
    send_admin_notification(
        AdminNotification.EventType.NEW_ORDER, "New order from Kofi Mensah"
    )

    response = client.get(reverse("admin_portal:admin_notification_dropdown"))

    assert response.status_code == 403
    assert b"Kofi Mensah" not in response.content


@pytest.mark.django_db
def test_a_non_staff_user_cannot_clear_the_admin_bell(client):
    """The other half: a non-staff POST could silently zero the admin's
    unread badge -- a denial of awareness on a feature whose entire
    reason for existing is that paid orders were going unseen."""
    from django.contrib.auth import get_user_model
    from django.urls import reverse

    user = get_user_model().objects.create_user(
        username="nosy-customer-2", password="Passw0rd!", is_staff=False
    )
    client.force_login(user)
    send_admin_notification(AdminNotification.EventType.NEW_ORDER, "one")

    response = client.post(reverse("admin_portal:admin_notification_mark_all_read"))

    assert response.status_code == 403
    assert AdminNotification.unread_count() == 1


@pytest.mark.django_db
def test_a_staff_user_without_verified_2fa_is_refused(client):
    """is_admin_portal_staff is is_staff AND is_verified(). A staff
    session that never completed 2FA must not reach admin data -- the
    same mandatory-2FA guarantee Django Admin itself carries here.

    This is also why the hole went unnoticed: every bell test created a
    bare is_staff user, so none of them ever exercised the gate.

    The platform redirects to 2FA setup rather than returning a bare 403
    -- better behaviour than a dead end, and the assertion follows the
    real behaviour rather than the one first assumed. What matters is
    that the request does not succeed and no admin data comes back."""
    from django.contrib.auth import get_user_model
    from django.urls import reverse

    user = get_user_model().objects.create_user(
        username="unverified-staff", password="Passw0rd!", is_staff=True
    )
    client.force_login(user)  # logged in, but no 2FA device in session

    response = client.get(reverse("admin_portal:admin_notification_dropdown"))

    assert response.status_code != 200
    assert "two_factor/setup" in response["Location"]


# --- Task 67c: where a bell notification takes you ----------------------------


@pytest.mark.django_db
def test_a_payment_issue_notification_links_to_the_payments_screen():
    """Before this it showed the text with nowhere to click, so the admin had
    to find the screen themselves."""
    from django.urls import reverse

    notification = AdminNotification.objects.create(
        event_type=AdminNotification.EventType.PAYMENT_ISSUE,
        message="Payment reg-abc needs attention",
    )

    assert notification.link_url == reverse("admin_portal:payment_issue_list")


@pytest.mark.django_db
def test_an_sms_credit_notification_has_nowhere_to_link():
    notification = AdminNotification.objects.create(
        event_type=AdminNotification.EventType.SMS_CREDIT,
        message="SMS credit has run out",
    )

    assert notification.link_url is None
