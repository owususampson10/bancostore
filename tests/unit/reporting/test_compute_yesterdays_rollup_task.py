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
from apps.reporting.models import DailyOrderRollup
from apps.reporting.tasks import (
    GAP_LOOKBACK_DAYS,
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


def _seed_steady_state_history(*, target_date, missing_dates=()):
    """Creates a DailyOrderRollup row for every date in this task's own
    [target_date - GAP_LOOKBACK_DAYS + 1, target_date] lookback window,
    except those in missing_dates -- simulates a system that's already
    been running the nightly rollup steadily, which is the realistic
    starting condition most of these tests want (a truly empty history
    is its own legitimate first-boot scenario, covered separately by
    test_caps_the_number_of_days_processed_in_a_single_run below, which
    deliberately seeds nothing)."""
    window_start = target_date - timedelta(days=GAP_LOOKBACK_DAYS - 1)
    missing = set(missing_dates)
    rows = []
    current = window_start
    while current <= target_date:
        if current not in missing:
            rows.append(DailyOrderRollup(date=current))
        current += timedelta(days=1)
    DailyOrderRollup.objects.bulk_create(rows)


@pytest.mark.django_db
def test_targets_yesterday_not_today():
    with patch("apps.reporting.tasks.timezone.now", return_value=_FIXED_NOW):
        yesterday = (_FIXED_NOW - timedelta(days=1)).date()
        _seed_steady_state_history(target_date=yesterday, missing_dates=[yesterday])

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
        _seed_steady_state_history(
            target_date=yesterday.date(), missing_dates=[yesterday.date()]
        )
        compute_yesterdays_rollup()

    rollup = DailyOrderRollup.objects.get(date=yesterday.date())
    assert rollup.revenue == Decimal("450.00")


@pytest.mark.django_db
def test_computes_every_missing_date_within_the_lookback_window():
    # A 3-day gap ending at "yesterday" -- e.g. an admin set
    # REPORT_ROLLUP_INTERVAL_DAYS=4, or Celery Beat missed a couple of
    # triggers. Every day in the gap must still get its own row.
    with patch("apps.reporting.tasks.timezone.now", return_value=_FIXED_NOW):
        yesterday = (_FIXED_NOW - timedelta(days=1)).date()
        gap = [yesterday - timedelta(days=offset) for offset in range(0, 3)]
        _seed_steady_state_history(target_date=yesterday, missing_dates=gap)

        result = compute_yesterdays_rollup()

    assert result["rollup_dates"] == sorted(d.isoformat() for d in gap)
    assert result["truncated"] is False
    for missing_date in gap:
        assert DailyOrderRollup.objects.filter(date=missing_date).exists()


@pytest.mark.django_db
def test_a_newer_manual_backfill_does_not_hide_an_older_gap():
    """CodeRabbit-caught real bug in an earlier version of this task: it
    used to resume from ReportRollupRun's MAX(succeeded rollup_date), so
    an out-of-band `backfill_report_rollup <newer date>` run (creating a
    DailyOrderRollup row for a date AFTER an unresolved older gap) would
    make the scheduled task's cursor jump past that older gap forever.
    This task is now stateless -- it checks DailyOrderRollup's own rows
    directly, so a manually-filled newer date can't hide an older one."""
    with patch("apps.reporting.tasks.timezone.now", return_value=_FIXED_NOW):
        yesterday = (_FIXED_NOW - timedelta(days=1)).date()
        older_gap = yesterday - timedelta(days=10)
        # Steady-state history except the older gap -- the newer dates
        # (including yesterday) already have rows, as if a manual
        # backfill had filled everything except this one older date.
        _seed_steady_state_history(target_date=yesterday, missing_dates=[older_gap])

        result = compute_yesterdays_rollup()

    assert result["rollup_dates"] == [older_gap.isoformat()]
    assert DailyOrderRollup.objects.filter(date=older_gap).exists()


@pytest.mark.django_db
def test_caps_the_number_of_days_processed_in_a_single_run():
    with patch("apps.reporting.tasks.timezone.now", return_value=_FIXED_NOW):
        yesterday = (_FIXED_NOW - timedelta(days=1)).date()
        # Deliberately seed nothing -- more missing dates
        # (GAP_LOOKBACK_DAYS) than the per-run cap (MAX_ROLLUP_DAYS_PER_RUN)
        # allows, e.g. this task's very first run ever against a live system.
        result = compute_yesterdays_rollup()

    assert len(result["rollup_dates"]) == MAX_ROLLUP_DAYS_PER_RUN
    assert result["truncated"] is True
    # Most-recent-missing-first: yesterday and the MAX_ROLLUP_DAYS_PER_RUN-1
    # days before it get processed, not the oldest days in the window --
    # a long-past gap must never starve "yesterday" of being computed.
    assert result["rollup_dates"][-1] == yesterday.isoformat()
    assert (
        result["rollup_dates"][0]
        == (yesterday - timedelta(days=MAX_ROLLUP_DAYS_PER_RUN - 1)).isoformat()
    )
    oldest_in_window = yesterday - timedelta(days=GAP_LOOKBACK_DAYS - 1)
    assert not DailyOrderRollup.objects.filter(date=oldest_in_window).exists()


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
