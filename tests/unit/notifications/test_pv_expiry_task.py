from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache

import pytest
from django_celery_beat.models import PeriodicTask

from apps.binary_tree.models import BinaryTreeEdge
from apps.commissions.tasks import BINARY_BONUS_LOCK_KEY, MATCHING_BONUS_LOCK_KEY
from apps.distributors.models import Distributor
from apps.notifications.models import (
    Notification,
    NotificationCycleFailure,
    NotificationCycleRun,
)
from apps.notifications.tasks import (
    PV_EXPIRY_LOCK_KEY,
    PV_EXPIRY_TASK_NAME,
    send_pv_expiry_notifications,
)
from apps.orders.tasks import AUTO_CANCEL_LOCK_KEY
from apps.pv_ledger.models import PvDailyBucket

User = get_user_model()
_phone_seq = count(1)

RUN_AT = datetime(2020, 3, 10, 10, 0, tzinfo=dt_timezone.utc)
TODAY = RUN_AT.date()
EXPIRY_DAYS = 180
WARNING_DAYS = 14


def _make_distributor():
    phone = f"+233255{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _bucket(distributor, leg, d, pv):
    return PvDailyBucket.objects.create(distributor=distributor, leg=leg, date=d, pv=pv)


@pytest.fixture(autouse=True)
def _clear_lock():
    cache.delete(PV_EXPIRY_LOCK_KEY)
    yield
    cache.delete(PV_EXPIRY_LOCK_KEY)


@pytest.mark.django_db(transaction=True)
def test_notifies_a_distributor_whose_pv_is_nearing_expiry():
    distributor = _make_distributor()
    bucket_date = TODAY - timedelta(days=EXPIRY_DAYS - (WARNING_DAYS - 1))
    _bucket(distributor, BinaryTreeEdge.Leg.LEFT, bucket_date, 200)

    with patch("apps.notifications.tasks.timezone.now", return_value=RUN_AT):
        result = send_pv_expiry_notifications()

    assert result["notified"] == 1
    assert Notification.objects.filter(
        distributor=distributor, event_type=Notification.EventType.PV_EXPIRING
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_does_not_notify_a_distributor_whose_pv_is_not_yet_nearing_expiry():
    distributor = _make_distributor()
    bucket_date = TODAY - timedelta(days=10)  # Far from the 180-day expiry.
    _bucket(distributor, BinaryTreeEdge.Leg.LEFT, bucket_date, 200)

    with patch("apps.notifications.tasks.timezone.now", return_value=RUN_AT):
        result = send_pv_expiry_notifications()

    assert result["notified"] == 0
    assert Notification.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_does_not_re_notify_within_the_warning_window():
    distributor = _make_distributor()
    bucket_date = TODAY - timedelta(days=EXPIRY_DAYS - (WARNING_DAYS - 1))
    _bucket(distributor, BinaryTreeEdge.Leg.LEFT, bucket_date, 200)

    with patch("apps.notifications.tasks.timezone.now", return_value=RUN_AT):
        first = send_pv_expiry_notifications()
        cache.delete(PV_EXPIRY_LOCK_KEY)
        second = send_pv_expiry_notifications()

    assert first["notified"] == 1
    assert second["notified"] == 0
    assert Notification.objects.filter(distributor=distributor).count() == 1


@pytest.mark.django_db(transaction=True)
def test_persists_an_audit_record_for_the_cycle():
    distributor = _make_distributor()
    bucket_date = TODAY - timedelta(days=EXPIRY_DAYS - (WARNING_DAYS - 1))
    _bucket(distributor, BinaryTreeEdge.Leg.LEFT, bucket_date, 200)

    with patch("apps.notifications.tasks.timezone.now", return_value=RUN_AT):
        send_pv_expiry_notifications()

    cycle_run = NotificationCycleRun.objects.get(run_at=RUN_AT)
    assert cycle_run.evaluated == 1
    assert cycle_run.notified == 1
    assert cycle_run.failed == 0


@pytest.mark.django_db(transaction=True)
def test_a_second_concurrent_trigger_is_skipped_while_the_lock_is_held():
    cache.add(PV_EXPIRY_LOCK_KEY, "1", 300)

    result = send_pv_expiry_notifications()

    assert result == {"skipped": True, "reason": "previous cycle still in progress"}


@pytest.mark.django_db(transaction=True)
def test_lock_is_released_after_a_successful_run():
    with patch("apps.notifications.tasks.timezone.now", return_value=RUN_AT):
        send_pv_expiry_notifications()

    assert cache.get(PV_EXPIRY_LOCK_KEY) is None


@pytest.mark.django_db(transaction=True)
def test_every_distributor_failing_raises_and_persists_failures():
    distributor = _make_distributor()
    _bucket(distributor, BinaryTreeEdge.Leg.LEFT, TODAY, 200)

    with patch(
        "apps.notifications.tasks.get_carry_forward_summary",
        side_effect=RuntimeError("forced failure for this test"),
    ):
        with patch("apps.notifications.tasks.timezone.now", return_value=RUN_AT):
            with pytest.raises(RuntimeError, match="every one of"):
                send_pv_expiry_notifications()

    cycle_run = NotificationCycleRun.objects.get(run_at=RUN_AT)
    assert cycle_run.evaluated == 1
    assert cycle_run.failed == 1
    assert NotificationCycleFailure.objects.filter(cycle_run=cycle_run).count() == 1


@pytest.mark.django_db
def test_task_name_and_lock_key_are_distinct_from_other_batch_jobs():
    assert PV_EXPIRY_LOCK_KEY not in {
        BINARY_BONUS_LOCK_KEY,
        MATCHING_BONUS_LOCK_KEY,
        AUTO_CANCEL_LOCK_KEY,
    }


@pytest.mark.django_db
def test_seed_migration_created_a_real_periodic_task():
    task = PeriodicTask.objects.get(name=PV_EXPIRY_TASK_NAME)
    assert task.task == "apps.notifications.tasks.send_pv_expiry_notifications"
    assert task.enabled is True
