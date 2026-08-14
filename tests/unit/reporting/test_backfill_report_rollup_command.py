from datetime import date
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

import pytest

from apps.catalog.models import Category, Product
from apps.orders.models import Order, OrderItem
from apps.reporting.models import DailyOrderRollup


def _make_product(**overrides):
    category, _ = Category.objects.get_or_create(name="Wellness", slug="wellness")
    defaults = {
        "name": "Vitality Pulse Smart Ring",
        "category": category,
        "price": Decimal("450.00"),
        "pv_value": 60,
        "stock": 100,
        "is_active": True,
    }
    defaults.update(overrides)
    return Product.objects.create(**defaults)


@pytest.mark.django_db
def test_backfills_the_given_date():
    product = _make_product()
    target_date = date(2026, 8, 1)
    order = Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=Decimal("450.00"),
        delivery_fee=Decimal("0"),
        total=Decimal("450.00"),
        payment_reference="order-test-backfill",
        status=Order.Status.CONFIRMED,
    )
    Order.objects.filter(pk=order.pk).update(created_at=f"{target_date}T12:00:00Z")
    OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        quantity=1,
        unit_price=product.price,
        unit_pv=product.pv_value,
    )

    out = StringIO()
    call_command("backfill_report_rollup", "2026-08-01", stdout=out)

    rollup = DailyOrderRollup.objects.get(date=target_date)
    assert rollup.revenue == Decimal("450.00")
    assert "Recomputed report rollup for 2026-08-01" in out.getvalue()


@pytest.mark.django_db
def test_rejects_an_invalid_date_format():
    with pytest.raises(CommandError, match="Invalid date"):
        call_command("backfill_report_rollup", "not-a-date")
