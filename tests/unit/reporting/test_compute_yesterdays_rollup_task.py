from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.utils import timezone

import pytest
from constance import config
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from apps.catalog.models import Category, Product
from apps.orders.models import Order, OrderItem
from apps.reporting.models import DailyOrderRollup, ReportRollupRun
from apps.reporting.tasks import (
    MAX_ROLLUP_DAYS_PER_RUN,
    REPORT_ROLLUP_LOCK_KEY,
    REPORT_ROLLUP_TASK_NAME,
    compute_yesterdays_rollup,
)

# Fixed so a test run straddling real UTC midnight can never flip which
# calendar day is "yesterday" mid-test (a CodeRabbit-caught real
# flakiness gap) -- every date/timestamp in this file is derived from
# this one instant, not a fresh timezone.now() call.
_FIXED_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=dt_timezone.utc)


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
    with patch("apps.reporting.tasks.timezone.now", return_value=_FIXED_NOW):
        yesterday = (_FIXED_NOW - timedelta(days=1)).date()

        result = compute_yesterdays_rollup()

    assert result["rollup_dates"] == [yesterday.isoformat()]
    assert DailyOrderRollup.objects.filter(date=yesterday).exists()
    assert not DailyOrderRollup.objects.filter(date=_FIXED_NOW.date()).exists()


@pytest.mark.django_db
def test_actually_rolls_up_yesterdays_orders():
    product = _make_product()
    yesterday = _FIXED_NOW - timedelta(days=1)
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

    with patch("apps.reporting.tasks.timezone.now", return_value=_FIXED_NOW):
        compute_yesterdays_rollup()

    rollup = DailyOrderRollup.objects.get(date=yesterday.date())
    assert rollup.revenue == Decimal("450.00")


@pytest.mark.django_db
def test_computes_every_missing_date_since_the_last_successful_rollup():
    # Last successful rollup was 3 days before "yesterday" -- e.g. an
    # admin set REPORT_ROLLUP_INTERVAL_DAYS=4, or Celery Beat missed a
    # couple of triggers. Every day in between must still get its own row.
    with patch("apps.reporting.tasks.timezone.now", return_value=_FIXED_NOW):
        yesterday = (_FIXED_NOW - timedelta(days=1)).date()
        last_success = yesterday - timedelta(days=3)
        ReportRollupRun.objects.create(
            rollup_date=last_success,
            run_at=_FIXED_NOW - timedelta(days=4),
            succeeded=True,
        )

        result = compute_yesterdays_rollup()

    expected_dates = [
        (last_success + timedelta(days=offset)).isoformat() for offset in range(1, 4)
    ]
    assert result["rollup_dates"] == expected_dates
    assert result["truncated"] is False
    for date_str in expected_dates:
        assert DailyOrderRollup.objects.filter(date=date_str).exists()


@pytest.mark.django_db
def test_a_failed_prior_run_does_not_count_as_the_last_success():
    with patch("apps.reporting.tasks.timezone.now", return_value=_FIXED_NOW):
        yesterday = (_FIXED_NOW - timedelta(days=1)).date()
        ReportRollupRun.objects.create(
            rollup_date=yesterday - timedelta(days=1),
            run_at=_FIXED_NOW - timedelta(days=2),
            succeeded=False,
            error="boom",
        )
        older_success = yesterday - timedelta(days=5)
        ReportRollupRun.objects.create(
            rollup_date=older_success,
            run_at=_FIXED_NOW - timedelta(days=6),
            succeeded=True,
        )

        result = compute_yesterdays_rollup()

    # Walks forward from the last SUCCESSFUL rollup_date (5 days back),
    # not the more recent failed attempt -- 5 days get recomputed.
    assert len(result["rollup_dates"]) == 5


@pytest.mark.django_db
def test_caps_the_number_of_days_processed_in_a_single_run():
    with patch("apps.reporting.tasks.timezone.now", return_value=_FIXED_NOW):
        yesterday = (_FIXED_NOW - timedelta(days=1)).date()
        last_success = yesterday - timedelta(days=MAX_ROLLUP_DAYS_PER_RUN + 10)
        ReportRollupRun.objects.create(
            rollup_date=last_success,
            run_at=_FIXED_NOW - timedelta(days=MAX_ROLLUP_DAYS_PER_RUN + 11),
            succeeded=True,
        )

        result = compute_yesterdays_rollup()

    assert len(result["rollup_dates"]) == MAX_ROLLUP_DAYS_PER_RUN
    assert result["truncated"] is True
    assert result["rollup_dates"][0] == (last_success + timedelta(days=1)).isoformat()


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
