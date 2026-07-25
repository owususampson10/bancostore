from decimal import Decimal

from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product
from apps.orders.models import Order, OrderItem


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
        "payment_reference": "order-confirmation-view-test",
        "status": Order.Status.PENDING,
    }
    defaults.update(overrides)
    return Order.objects.create(**defaults)


@pytest.mark.django_db
def test_confirmed_order_shows_the_confirmed_state(client):
    product = _make_product()
    order = _make_order(status=Order.Status.CONFIRMED)
    OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        quantity=1,
        unit_price=product.price,
        unit_pv=product.pv_value,
    )

    response = client.get(
        reverse("orders:order_confirmation", args=[order.payment_reference])
    )

    content = response.content.decode()
    assert "Order Confirmed" in content
    assert product.name in content
    assert "Item No Longer Available" not in content


@pytest.mark.django_db
def test_cancelled_order_shows_the_unavailable_state_with_no_fake_refund_promise(
    client,
):
    order = _make_order(status=Order.Status.CANCELLED)

    response = client.get(
        reverse("orders:order_confirmation", args=[order.payment_reference])
    )

    content = response.content.decode()
    assert "Item No Longer Available" in content
    assert "Order Confirmed" not in content
    # The Stitch mockup this was built from claimed an automated refund
    # ("typically processed within 3-5 business days") and a fake
    # Initiated/Processing/Completed tracker -- this codebase's actual
    # design decision (2026-07-25) is manual admin follow-up, no
    # automated refund pipeline exists, so neither claim may appear here.
    assert "3-5 business days" not in content
    assert "Processing" not in content


@pytest.mark.django_db
def test_pending_order_shows_a_waiting_state_not_the_old_payment_next_step_copy(
    client,
):
    order = _make_order(status=Order.Status.PENDING)

    response = client.get(
        reverse("orders:order_confirmation", args=[order.payment_reference])
    )

    content = response.content.decode()
    # Superseded by Task 17d: payment now actually happens, so the old
    # "we're not ready to take you there automatically yet" copy would be
    # actively wrong to still show once this page can be reached after a
    # real (if not-yet-confirmed) payment attempt.
    assert "not ready to take you there automatically" not in content
    assert "Awaiting Payment Confirmation" in content


@pytest.mark.django_db
def test_a_later_lifecycle_status_does_not_show_the_awaiting_payment_copy(client):
    # CodeRabbit (PR #27): Order.Status also has processing/dispatched/
    # delivered/refunded (Task 18's scope) -- this page is reachable by
    # reference at any time, so a dispatched order landing in the bare
    # {% else %} branch would tell the customer their payment isn't
    # confirmed yet, which is actively wrong once it's been dispatched.
    order = _make_order(status=Order.Status.DISPATCHED)

    response = client.get(
        reverse("orders:order_confirmation", args=[order.payment_reference])
    )

    content = response.content.decode()
    assert "Awaiting Payment Confirmation" not in content
    assert "Item No Longer Available" not in content


@pytest.mark.django_db
def test_unknown_reference_404s(client):
    response = client.get(
        reverse("orders:order_confirmation", args=["order-does-not-exist"])
    )
    assert response.status_code == 404


@pytest.mark.django_db
def test_confirmed_order_with_more_line_items_does_not_add_more_queries(client):
    # code-review-and-quality (2026-07-25): the confirmed state iterates
    # order.items.all and reads item.product.primary_image per item --
    # Product.primary_image's own docstring requires a caller's
    # prefetch_related("images") to avoid a query per call, and without
    # prefetching items__product too, item.product would also be a query
    # per item. Proven the same way tests/feature/catalog/test_product_
    # admin.py proves its own N+1 fix: query count must stay flat as item
    # count grows, not scale with it.
    from django.db import connection, reset_queries
    from django.test.utils import CaptureQueriesContext

    order_one_item = _make_order(payment_reference="order-n-plus-one-a")
    order_one_item.status = Order.Status.CONFIRMED
    order_one_item.save(update_fields=["status"])
    OrderItem.objects.create(
        order=order_one_item,
        product=_make_product(name="Solo Product"),
        product_name="Solo Product",
        quantity=1,
        unit_price=Decimal("450.00"),
        unit_pv=60,
    )

    order_many_items = _make_order(payment_reference="order-n-plus-one-b")
    order_many_items.status = Order.Status.CONFIRMED
    order_many_items.save(update_fields=["status"])
    for i in range(5):
        product = _make_product(name=f"Product {i}")
        OrderItem.objects.create(
            order=order_many_items,
            product=product,
            product_name=product.name,
            quantity=1,
            unit_price=Decimal("450.00"),
            unit_pv=60,
        )

    reset_queries()
    with CaptureQueriesContext(connection) as one_item:
        response = client.get(
            reverse(
                "orders:order_confirmation", args=[order_one_item.payment_reference]
            )
        )
    assert response.status_code == 200

    reset_queries()
    with CaptureQueriesContext(connection) as many_items:
        response = client.get(
            reverse(
                "orders:order_confirmation", args=[order_many_items.payment_reference]
            )
        )
    assert response.status_code == 200

    # A tolerance of 2 absorbs incidental non-N+1 query variance (e.g.
    # silk's own housekeeping), matching test_product_admin.py's own
    # documented reasoning -- an N+1 regression here would add ~5 more
    # queries (one per extra item), not 0-2.
    assert len(many_items) <= len(one_item) + 2
