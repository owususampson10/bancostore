from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product
from apps.orders.models import Order, OrderItem

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


def _make_order(**overrides):
    defaults = {
        "full_name": "Ama Mensah",
        "phone_number": "+233241234567",
        "email": "ama@example.test",
        "delivery_method": Order.DeliveryMethod.HOME_DELIVERY,
        "delivery_zone": Order.DeliveryZone.ACCRA,
        "address": "12 High St",
        "area": "Osu",
        "subtotal": Decimal("450.00"),
        "delivery_fee": Decimal("50.00"),
        "total": Decimal("500.00"),
        "pv_earned": 60,
        "payment_reference": f"order-history-test-{Order.objects.count()}",
        "status": Order.Status.CONFIRMED,
    }
    defaults.update(overrides)
    return Order.objects.create(**defaults)


def _make_order_item(order, **overrides):
    product = overrides.pop("product", None) or _make_product(
        name=f"Product {OrderItem.objects.count()}"
    )
    defaults = {
        "order": order,
        "product": product,
        "product_name": product.name,
        "quantity": 1,
        "unit_price": product.price,
        "unit_pv": product.pv_value,
    }
    defaults.update(overrides)
    return OrderItem.objects.create(**defaults)


@pytest.mark.django_db
def test_requires_login(client):
    response = client.get(reverse("orders:order_history"))

    assert response.status_code == 302
    assert "/accounts/login/" in response.url or "login" in response.url


@pytest.mark.django_db
def test_shows_the_logged_in_customers_own_orders(client):
    customer = User.objects.create_user(username="ama@example.test", password="pw")
    order = _make_order(customer=customer, payment_reference="order-own-1")
    _make_order_item(order, product=_make_product(name="Vitality Pulse Smart Ring"))
    client.force_login(customer)

    response = client.get(reverse("orders:order_history"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Vitality Pulse Smart Ring" in body
    assert "500.00" in body
    assert "60" in body  # PV earned


@pytest.mark.django_db
def test_never_shows_another_users_orders(client):
    own = User.objects.create_user(username="ama@example.test", password="pw")
    other = User.objects.create_user(username="kofi@example.test", password="pw")
    _make_order(customer=own, payment_reference="order-own-2")
    other_order = _make_order(
        customer=other, payment_reference="order-other-1", full_name="Kofi Boateng"
    )
    _make_order_item(other_order, product=_make_product(name="Other Person's Product"))
    client.force_login(own)

    response = client.get(reverse("orders:order_history"))

    body = response.content.decode()
    assert "Other Person's Product" not in body
    assert "order-other-1" not in body


@pytest.mark.django_db
def test_guest_checkout_orders_never_appear_for_any_logged_in_user(client):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    _make_order(customer=None, payment_reference="order-guest-1")
    client.force_login(user)

    response = client.get(reverse("orders:order_history"))

    assert "order-guest-1" not in response.content.decode()


@pytest.mark.django_db
def test_a_distributor_sees_their_own_orders_too():
    """Order.customer is a plain FK to the shared User model, not
    Distributor-specific -- this view must work identically for a
    distributor-as-customer, with no separate code path."""
    from django.contrib.auth.models import Group
    from django.test import Client

    from apps.distributors.models import Distributor

    phone = "+233241234567"
    user = User.objects.create_user(username=phone, password="pw")
    distributor_group, _ = Group.objects.get_or_create(name="distributor")
    user.groups.add(distributor_group)
    Distributor.objects.create(user=user, phone_number=phone)
    order = _make_order(customer=user, payment_reference="order-distributor-1")
    _make_order_item(order, product=_make_product(name="Distributor Own Purchase"))

    client = Client()
    client.force_login(user)
    response = client.get(reverse("orders:order_history"))

    assert "Distributor Own Purchase" in response.content.decode()


@pytest.mark.django_db
def test_pagination_is_stable_when_created_at_collides(client):
    """Mirrors Task 15d's own precedent: -created_at alone can skip/
    duplicate a row across a page boundary on a timestamp collision --
    -pk must be the tie-breaker."""
    from django.utils import timezone

    customer = User.objects.create_user(username="ama@example.test", password="pw")
    same_instant = timezone.now()
    orders = []
    for i in range(25):
        order = _make_order(customer=customer, payment_reference=f"order-page-{i}")
        Order.objects.filter(pk=order.pk).update(created_at=same_instant)
        orders.append(order)
    client.force_login(customer)

    page_one = client.get(reverse("orders:order_history"))
    page_two = client.get(reverse("orders:order_history"), {"page": 2})

    refs_one = {o.payment_reference for o in page_one.context["page_obj"].object_list}
    refs_two = {o.payment_reference for o in page_two.context["page_obj"].object_list}
    assert not (refs_one & refs_two)  # no duplicate across the boundary
    assert len(refs_one) + len(refs_two) == 25  # none skipped


@pytest.mark.django_db
def test_an_invalid_page_number_does_not_500(client):
    customer = User.objects.create_user(username="ama@example.test", password="pw")
    _make_order(customer=customer)
    client.force_login(customer)

    response = client.get(reverse("orders:order_history"), {"page": "not-a-number"})

    assert response.status_code == 200


@pytest.mark.django_db
def test_more_line_items_does_not_add_more_queries(client):
    """Mirrors test_order_confirmation_view.py's own N+1 proof exactly --
    the list page iterates order.items.all and reads item.product's
    image per item, same prefetch requirement. Two separate customers,
    each with exactly one order, isolates item-count as the only
    variable -- a before/after on the SAME list would also change order
    count, confounding the comparison."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    solo_customer = User.objects.create_user(
        username="solo@example.test", password="pw"
    )
    solo_order = _make_order(customer=solo_customer, payment_reference="order-np1-solo")
    _make_order_item(solo_order, product=_make_product(name="Solo Product"))

    bulk_customer = User.objects.create_user(
        username="bulk@example.test", password="pw"
    )
    bulk_order = _make_order(customer=bulk_customer, payment_reference="order-np1-bulk")
    for i in range(5):
        _make_order_item(bulk_order, product=_make_product(name=f"Bulk Product {i}"))

    client.force_login(solo_customer)
    with CaptureQueriesContext(connection) as one_item:
        response = client.get(reverse("orders:order_history"))
    assert response.status_code == 200
    client.logout()

    client.force_login(bulk_customer)
    with CaptureQueriesContext(connection) as many_items:
        response = client.get(reverse("orders:order_history"))
    assert response.status_code == 200

    assert len(many_items) <= len(one_item) + 2


# --- Task 25 (doubt-driven-development finding): a NEW, ownership-scoped ---
# --- detail view -- deliberately NOT a reuse of order_confirmation_view,  ---
# --- whose "no ownership check, unguessable token" design is only safe    ---
# --- because it's reachable solely via a one-time post-checkout redirect, ---
# --- not a durable, revisitable, bookmarkable list page. ------------------


@pytest.mark.django_db
def test_order_detail_requires_login(client):
    order = _make_order()

    response = client.get(reverse("orders:order_detail", args=[order.pk]))

    assert response.status_code == 302
    assert "login" in response.url


@pytest.mark.django_db
def test_order_detail_shows_the_owners_own_order(client):
    customer = User.objects.create_user(username="ama@example.test", password="pw")
    order = _make_order(customer=customer)
    _make_order_item(order, product=_make_product(name="Vitality Pulse Smart Ring"))
    client.force_login(customer)

    response = client.get(reverse("orders:order_detail", args=[order.pk]))

    assert response.status_code == 200
    assert "Vitality Pulse Smart Ring" in response.content.decode()


@pytest.mark.django_db
def test_order_detail_shows_items_for_a_delivered_order(client):
    """Regression guard: order_created.html originally only rendered the
    item/delivery breakdown when status == "confirmed" -- built for the
    one-time post-checkout redirect, where that's the only realistic
    terminal state. Reused here for a permanent history page reachable at
    any lifecycle stage, a "delivered" order (which spends most of its
    life past "confirmed") showed a bare "Order Placed" message with no
    items at all, found via real-browser verification."""
    customer = User.objects.create_user(username="ama@example.test", password="pw")
    order = _make_order(customer=customer, status=Order.Status.DELIVERED)
    _make_order_item(order, product=_make_product(name="Vitality Pulse Smart Ring"))
    client.force_login(customer)

    response = client.get(reverse("orders:order_detail", args=[order.pk]))

    body = response.content.decode()
    assert "Vitality Pulse Smart Ring" in body
    assert order.get_delivery_method_display() in body


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status,expected_text",
    [
        (Order.Status.PROCESSING, "Order Processing"),
        (Order.Status.DISPATCHED, "Order Dispatched"),
        (Order.Status.DELIVERED, "Order Delivered"),
        (Order.Status.REFUNDED, "Order Refunded"),
    ],
)
def test_order_detail_header_matches_status_past_confirmation(
    client, status, expected_text
):
    """Code review finding: broadening the item/delivery block to show for
    any non-pending status (previous test) left the HEADER block still only
    written for confirmed/cancelled/pending -- every other real status fell
    into a generic "Order Placed / being processed" fallback, which is
    wrong for e.g. a delivered or refunded order. Each status now needs its
    own accurate header."""
    customer = User.objects.create_user(username="ama@example.test", password="pw")
    order = _make_order(customer=customer, status=status)
    client.force_login(customer)

    response = client.get(reverse("orders:order_detail", args=[order.pk]))

    body = response.content.decode()
    assert expected_text in body
    assert "being processed" not in body


@pytest.mark.django_db
def test_order_detail_cancelled_copy_does_not_assume_stock_unavailability(client):
    """A cancelled order isn't only reachable via the insufficient-stock
    auto-cancel path anymore (apps/orders/services.py::cancel_or_refund_order
    lets an admin cancel for any reason) -- the header must not claim "an
    item became unavailable" for a cancellation that had nothing to do with
    stock."""
    customer = User.objects.create_user(username="ama@example.test", password="pw")
    order = _make_order(customer=customer, status=Order.Status.CANCELLED)
    client.force_login(customer)

    response = client.get(reverse("orders:order_detail", args=[order.pk]))

    body = response.content.decode()
    assert "Order Cancelled" in body
    assert "item in your order became unavailable" not in body


@pytest.mark.django_db
def test_order_detail_404s_for_another_users_order(client):
    """The core IDOR proof: a logged-in user must never be able to view
    another user's order by pk, even though pks are small sequential
    integers (unlike payment_reference's 128-bit UUID)."""
    own = User.objects.create_user(username="ama@example.test", password="pw")
    other = User.objects.create_user(username="kofi@example.test", password="pw")
    other_order = _make_order(customer=other, full_name="Kofi Boateng")
    client.force_login(own)

    response = client.get(reverse("orders:order_detail", args=[other_order.pk]))

    assert response.status_code == 404


@pytest.mark.django_db
def test_order_detail_404s_for_a_guest_orders_pk(client):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    guest_order = _make_order(customer=None)
    client.force_login(user)

    response = client.get(reverse("orders:order_detail", args=[guest_order.pk]))

    assert response.status_code == 404


@pytest.mark.django_db
def test_order_detail_404s_for_a_nonexistent_pk(client):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)

    response = client.get(reverse("orders:order_detail", args=[999999]))

    assert response.status_code == 404


@pytest.mark.django_db
def test_order_detail_never_renders_the_admin_tracking_note(client):
    customer = User.objects.create_user(username="ama@example.test", password="pw")
    order = _make_order(
        customer=customer, tracking_note="Internal: customer called twice, irate."
    )
    client.force_login(customer)

    response = client.get(reverse("orders:order_detail", args=[order.pk]))

    assert "Internal: customer called twice, irate." not in response.content.decode()
