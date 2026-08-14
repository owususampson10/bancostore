from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.utils import timezone

import pytest
from constance import config
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from apps.catalog.models import Category, Product
from apps.orders.models import Order, OrderItem
from apps.reporting.models import DailyOrderRollup
from apps.reporting.tasks import (
    REPORT_ROLLUP_LOCK_KEY,
    REPORT_ROLLUP_TASK_NAME,
    compute_yesterdays_rollup,
)


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
def test_targets_yesterday_not_today():
    yesterday = (timezone.now() - timedelta(days=1)).date()

    result = compute_yesterdays_rollup()

    assert result["rollup_date"] == yesterday.isoformat()
    assert DailyOrderRollup.objects.filter(date=yesterday).exists()
    assert not DailyOrderRollup.objects.filter(date=timezone.now().date()).exists()


@pytest.mark.django_db
def test_actually_rolls_up_yesterdays_orders():
    product = _make_product()
    yesterday = timezone.now() - timedelta(days=1)
    order = Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=Decimal("450.00"),
        delivery_fee=Decimal("0"),
        total=Decimal("450.00"),
        payment_reference="order-test-yesterday-rollup",
        status=Order.Status.CONFIRMED,
    )
    Order.objects.filter(pk=order.pk).update(created_at=yesterday)
    OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        quantity=1,
        unit_price=product.price,
        unit_pv=product.pv_value,
    )

    compute_yesterdays_rollup()

    rollup = DailyOrderRollup.objects.get(date=yesterday.date())
    assert rollup.revenue == Decimal("450.00")


@pytest.mark.django_db
def test_interval_self_syncs_from_the_constance_setting():
    stale_schedule = IntervalSchedule.objects.create(
        every=3, period=IntervalSchedule.DAYS
    )
    task = PeriodicTask.objects.get(name=REPORT_ROLLUP_TASK_NAME)
    task.interval = stale_schedule
    task.save(update_fields=["interval"])
    config.REPORT_ROLLUP_INTERVAL_DAYS = 2

    compute_yesterdays_rollup()

    task = PeriodicTask.objects.get(name=REPORT_ROLLUP_TASK_NAME)
    assert task.interval.every == 2
    assert task.interval.period == IntervalSchedule.DAYS


@pytest.mark.django_db
def test_a_held_lock_skips_the_run_instead_of_computing_twice():
    cache.add(REPORT_ROLLUP_LOCK_KEY, "1", 60)
    try:
        result = compute_yesterdays_rollup()
    finally:
        cache.delete(REPORT_ROLLUP_LOCK_KEY)

    assert result == {"skipped": True, "reason": "previous run still in progress"}
    yesterday = (timezone.now() - timedelta(days=1)).date()
    assert not DailyOrderRollup.objects.filter(date=yesterday).exists()


@pytest.mark.django_db
def test_the_lock_is_released_even_when_the_rollup_raises():
    with patch(
        "apps.reporting.tasks.compute_daily_rollup", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError, match="boom"):
            compute_yesterdays_rollup()

    assert cache.get(REPORT_ROLLUP_LOCK_KEY) is None


@pytest.mark.django_db
def test_missing_periodic_task_row_does_not_break_the_run():
    PeriodicTask.objects.filter(name=REPORT_ROLLUP_TASK_NAME).delete()

    result = compute_yesterdays_rollup()

    assert result["succeeded"] is True
