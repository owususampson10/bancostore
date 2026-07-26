from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

import apps.orders.tasks as tasks_module
from apps.orders.models import Order, OrderCycleFailure, OrderCycleRun
from apps.orders.services import _auto_cancel_pending_order
from apps.orders.tasks import (
    AUTO_CANCEL_LOCK_KEY,
    AUTO_CANCEL_TASK_NAME,
    auto_cancel_unpaid_orders,
)

_ref_seq = count(1)

RUN_AT = datetime(2026, 7, 27, 10, 0, tzinfo=dt_timezone.utc)


def _make_order(
    *, status=Order.Status.PENDING, created_at=None, total=Decimal("450.00")
):
    order = Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=total,
        delivery_fee=Decimal("0"),
        total=total,
        payment_reference=f"order-test-ref-{next(_ref_seq)}",
        status=status,
    )
    if created_at is not None:
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        order.refresh_from_db()
    return order


# ---------------------------------------------------------------------------
# apps.orders.services._auto_cancel_pending_order
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_cancels_a_pending_order(mock_sms, mock_mail):
    order = _make_order(status=Order.Status.PENDING)

    result = _auto_cancel_pending_order(order.pk)

    order.refresh_from_db()
    assert result is True
    assert order.status == Order.Status.CANCELLED


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_a_confirmed_order_is_left_untouched(mock_sms, mock_mail):
    order = _make_order(status=Order.Status.CONFIRMED)

    result = _auto_cancel_pending_order(order.pk)

    order.refresh_from_db()
    assert result is False
    assert order.status == Order.Status.CONFIRMED
    mock_sms.assert_not_called()


@pytest.mark.django_db
def test_a_missing_order_returns_false():
    assert _auto_cancel_pending_order(999999) is False


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_a_real_cancellation_sends_a_notification(mock_sms, mock_mail):
    order = _make_order(status=Order.Status.PENDING)

    _auto_cancel_pending_order(order.pk)

    mock_sms.assert_called_once()
    mock_mail.assert_called_once()


# ---------------------------------------------------------------------------
# apps.orders.tasks.auto_cancel_unpaid_orders
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.tasks.timezone.now")
def test_cancels_an_order_older_than_the_cutoff(mock_now, mock_sms, mock_mail):
    mock_now.return_value = RUN_AT
    stale = _make_order(
        status=Order.Status.PENDING, created_at=RUN_AT - timedelta(hours=25)
    )

    auto_cancel_unpaid_orders()

    stale.refresh_from_db()
    assert stale.status == Order.Status.CANCELLED


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.tasks.timezone.now")
def test_leaves_a_fresher_pending_order_untouched(mock_now, mock_sms, mock_mail):
    mock_now.return_value = RUN_AT
    fresh = _make_order(
        status=Order.Status.PENDING, created_at=RUN_AT - timedelta(hours=1)
    )

    auto_cancel_unpaid_orders()

    fresh.refresh_from_db()
    assert fresh.status == Order.Status.PENDING


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.tasks.timezone.now")
def test_never_touches_a_confirmed_order_regardless_of_age(
    mock_now, mock_sms, mock_mail
):
    mock_now.return_value = RUN_AT
    old_confirmed = _make_order(
        status=Order.Status.CONFIRMED, created_at=RUN_AT - timedelta(days=30)
    )

    auto_cancel_unpaid_orders()

    old_confirmed.refresh_from_db()
    assert old_confirmed.status == Order.Status.CONFIRMED


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.tasks.timezone.now")
def test_persists_an_audit_record_for_the_cycle(mock_now, mock_sms, mock_mail):
    mock_now.return_value = RUN_AT
    _make_order(status=Order.Status.PENDING, created_at=RUN_AT - timedelta(hours=25))
    _make_order(status=Order.Status.PENDING, created_at=RUN_AT - timedelta(hours=1))

    auto_cancel_unpaid_orders()

    run = OrderCycleRun.objects.get(run_at=RUN_AT)
    assert run.evaluated == 1
    assert run.cancelled == 1
    assert run.failed == 0


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.tasks.timezone.now")
def test_lock_is_released_after_a_successful_run(mock_now, mock_sms, mock_mail):
    mock_now.return_value = RUN_AT
    _make_order(status=Order.Status.PENDING, created_at=RUN_AT - timedelta(hours=25))

    auto_cancel_unpaid_orders()

    assert cache.get(AUTO_CANCEL_LOCK_KEY) is None


@pytest.mark.django_db
@patch("apps.orders.tasks.timezone.now")
def test_a_second_concurrent_trigger_is_skipped_while_the_lock_is_held(mock_now):
    mock_now.return_value = RUN_AT
    cache.add(AUTO_CANCEL_LOCK_KEY, "1", 60)

    result = auto_cancel_unpaid_orders()

    assert result == {"skipped": True, "reason": "previous cycle still in progress"}
    cache.delete(AUTO_CANCEL_LOCK_KEY)


@pytest.mark.django_db
@patch("apps.orders.tasks.timezone.now")
def test_every_order_failing_raises_and_persists_failures(mock_now):
    mock_now.return_value = RUN_AT
    order = _make_order(
        status=Order.Status.PENDING, created_at=RUN_AT - timedelta(hours=25)
    )

    with patch.object(
        tasks_module,
        "_auto_cancel_pending_order",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError):
            auto_cancel_unpaid_orders()

    run = OrderCycleRun.objects.get(run_at=RUN_AT)
    assert run.failed == 1
    failure = OrderCycleFailure.objects.get(cycle_run=run)
    assert failure.order_id == order.pk
    assert "boom" in failure.error
    assert cache.get(AUTO_CANCEL_LOCK_KEY) is None


@pytest.mark.django_db
def test_task_name_and_lock_key_are_distinct_from_other_batch_jobs():
    # Cheap tripwire against accidentally colliding with an existing
    # commission/withdrawal lock key or PeriodicTask name.
    assert AUTO_CANCEL_TASK_NAME == "auto-cancel-unpaid-orders"
    assert AUTO_CANCEL_LOCK_KEY == "orders:auto_cancel_lock"


@pytest.mark.django_db
def test_seed_migration_created_a_real_periodic_task():
    from django_celery_beat.models import PeriodicTask

    task = PeriodicTask.objects.select_related("interval").get(
        name=AUTO_CANCEL_TASK_NAME
    )
    assert task.task == "apps.orders.tasks.auto_cancel_unpaid_orders"
    assert task.interval.every == 30
    assert task.interval.period == "minutes"


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.tasks.timezone.now")
def test_query_count_stays_flat_per_order_regardless_of_eligible_count(
    mock_now, mock_sms, mock_mail
):
    """CodeRabbit (PR #31): the 'query count stays flat' acceptance
    criterion (Task 13's own established scale discipline -- no N+1 per
    eligible order) was checked off in tasks/todo.md with no test
    actually measuring it. Mirrors tests/unit/pv_ledger
    /test_purchase_increment.py::test_query_count_does_not_grow_with_
    ancestor_depth's own way of proving a scale-sensitive path stays
    flat: compares total query count at two different eligible-order
    counts and asserts the marginal per-order cost is a small constant,
    not something that grows with the total -- an N+1 would show up as
    a disproportionate jump between the two, not a linear one."""
    mock_now.return_value = RUN_AT

    for _ in range(2):
        _make_order(
            status=Order.Status.PENDING, created_at=RUN_AT - timedelta(hours=25)
        )
    with CaptureQueriesContext(connection) as ctx_small:
        auto_cancel_unpaid_orders()
    small_count = len(ctx_small.captured_queries)

    OrderCycleRun.objects.all().delete()
    for _ in range(10):
        _make_order(
            status=Order.Status.PENDING, created_at=RUN_AT - timedelta(hours=25)
        )
    with CaptureQueriesContext(connection) as ctx_large:
        auto_cancel_unpaid_orders()
    large_count = len(ctx_large.captured_queries)

    # 8 extra orders (10 - 2) between the two runs -- if per-order cost
    # were growing with the total eligible count (an N+1), this delta
    # would blow up well past a small per-order constant (lock+get,
    # status UPDATE, the simple_history INSERT it triggers -- a handful
    # of queries, not dozens) as the eligible count grows.
    per_order_cost = (large_count - small_count) / 8
    assert per_order_cost < 10
