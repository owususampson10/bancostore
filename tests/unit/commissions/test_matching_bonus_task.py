from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache

import pytest
from constance import config
from django_celery_beat.models import IntervalSchedule, PeriodicTask

import apps.commissions.tasks as tasks_module
from apps.commissions.models import CommissionCycleFailure, CommissionCycleRun
from apps.commissions.services import process_matching_bonus_for_distributor
from apps.commissions.tasks import (
    BINARY_BONUS_LOCK_KEY,
    MATCHING_BONUS_LOCK_KEY,
    MATCHING_BONUS_TASK_NAME,
    calculate_matching_bonus,
)
from apps.distributors.models import Distributor
from apps.pv_ledger.models import MonthlyPersonalPv
from apps.wallet.models import Wallet, WalletTransaction

User = get_user_model()
_phone_seq = count(1)

RUN_AT = datetime(2020, 3, 10, 10, 0, tzinfo=dt_timezone.utc)

# Note on test coverage scope: the generic cycle-runner mechanics (lock
# renewal under load, per-iteration TTL renewal, systemic-failure guard,
# audit-trail persistence on both success and raise, crontab-schedule
# preservation, non-positive-interval guard) are shared code
# (_run_commission_cycle / _sync_periodic_task_interval /
# _persist_cycle_audit_record in apps/commissions/tasks.py) already
# exhaustively covered by tests/unit/commissions/test_binary_bonus_task.py,
# which exercises the exact same functions via calculate_binary_bonus.
# Re-testing every one of those cases again here via calculate_matching_
# bonus would duplicate coverage of code that's already proven, not add
# any. This file focuses on what's genuinely specific to THIS task's
# wiring: which ids get selected, which per-distributor function is
# called, which lock key/interval setting/period unit it uses, and that
# it doesn't collide with Binary Bonus's own lock or schedule.


def _make_distributor(sponsor=None, rank=""):
    phone = f"+233246{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(
        user=user, phone_number=phone, sponsor=sponsor, rank=rank
    )


def _make_eligible(distributor, run_at=RUN_AT):
    MonthlyPersonalPv.objects.create(
        distributor=distributor,
        period=run_at.date().replace(day=1),
        pv=config.MIN_MONTHLY_PERSONAL_PV,
    )


def _make_binary_bonus_transaction(distributor, amount, run_at=RUN_AT, days_ago=1):
    from datetime import timedelta

    wallet, _ = Wallet.objects.get_or_create(distributor=distributor)
    txn = WalletTransaction.objects.create(
        wallet=wallet,
        amount=amount,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference=f"seed-{distributor.pk}-{amount}",
    )
    WalletTransaction.objects.filter(pk=txn.pk).update(
        created_at=run_at - timedelta(days=days_ago)
    )
    return txn


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_holds_one_run_at_and_credits_via_the_matching_bonus_reference_format(
    mock_now,
):
    mock_now.return_value = RUN_AT
    root = _make_distributor(rank="bronze")
    _make_eligible(root)
    level1 = _make_distributor(sponsor=root)
    _make_binary_bonus_transaction(level1, Decimal("200"))

    calculate_matching_bonus()

    txn = WalletTransaction.objects.get(
        wallet__distributor=root,
        transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS,
    )
    assert txn.reference == f"matching-bonus-{root.pk}-{RUN_AT.isoformat()}"
    assert txn.amount == Decimal("10.00")


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_only_iterates_ranked_distributors_with_a_direct_referral(mock_now):
    mock_now.return_value = RUN_AT
    eligible_root = _make_distributor(rank="bronze")
    _make_eligible(eligible_root)
    level1 = _make_distributor(sponsor=eligible_root)
    _make_binary_bonus_transaction(level1, Decimal("200"))

    ranked_but_no_downline = _make_distributor(rank="silver")
    _make_eligible(ranked_but_no_downline)

    downline_but_no_rank = _make_distributor()  # rank="" -- not evaluated
    _make_eligible(downline_but_no_rank)
    _make_distributor(sponsor=downline_but_no_rank)

    calculate_matching_bonus()

    assert WalletTransaction.objects.filter(
        wallet__distributor=eligible_root,
        transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS,
    ).exists()
    assert not Wallet.objects.filter(distributor=ranked_but_no_downline).exists()
    assert not Wallet.objects.filter(distributor=downline_but_no_rank).exists()


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_one_distributors_failure_does_not_abort_the_batch(mock_now):
    mock_now.return_value = RUN_AT
    failing = _make_distributor(rank="bronze")
    _make_eligible(failing)
    failing_level1 = _make_distributor(sponsor=failing)
    _make_binary_bonus_transaction(failing_level1, Decimal("200"))

    healthy = _make_distributor(rank="bronze")
    _make_eligible(healthy)
    healthy_level1 = _make_distributor(sponsor=healthy)
    _make_binary_bonus_transaction(healthy_level1, Decimal("200"))

    real_fn = process_matching_bonus_for_distributor

    def side_effect(distributor, run_at):
        if distributor.pk == failing.pk:
            raise RuntimeError("boom")
        return real_fn(distributor, run_at)

    with patch.object(
        tasks_module, "process_matching_bonus_for_distributor", side_effect=side_effect
    ):
        summary = calculate_matching_bonus()

    assert WalletTransaction.objects.filter(
        wallet__distributor=healthy,
        transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS,
    ).exists()
    assert summary["failed"] == 1
    assert summary["paid"] == 1


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_systemic_failure_raises_instead_of_a_quiet_summary(mock_now):
    mock_now.return_value = RUN_AT
    root = _make_distributor(rank="bronze")
    _make_eligible(root)
    _make_distributor(sponsor=root)

    with patch.object(
        tasks_module,
        "process_matching_bonus_for_distributor",
        side_effect=RuntimeError("systemic bug"),
    ):
        with pytest.raises(RuntimeError):
            calculate_matching_bonus()


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_persists_an_audit_record_tagged_with_its_own_job_name(mock_now):
    mock_now.return_value = RUN_AT
    root = _make_distributor(rank="bronze")
    _make_eligible(root)
    level1 = _make_distributor(sponsor=root)
    _make_binary_bonus_transaction(level1, Decimal("200"))

    calculate_matching_bonus()

    run = CommissionCycleRun.objects.get(
        job_name=MATCHING_BONUS_TASK_NAME, run_at=RUN_AT
    )
    assert run.evaluated == 1
    assert run.paid == 1
    assert run.total_amount == Decimal("10.00")
    assert not CommissionCycleFailure.objects.filter(cycle_run=run).exists()


@pytest.mark.django_db
def test_uses_its_own_lock_key_independent_of_binary_bonus():
    """The two jobs must never share a lock -- otherwise a Binary Bonus
    cycle in flight would incorrectly block Matching Bonus from running
    at all, and vice versa."""
    assert MATCHING_BONUS_LOCK_KEY != BINARY_BONUS_LOCK_KEY

    assert cache.add(BINARY_BONUS_LOCK_KEY, "1", 60)  # Binary Bonus "in flight"
    root = _make_distributor(rank="bronze")
    _make_eligible(root)
    level1 = _make_distributor(sponsor=root)
    _make_binary_bonus_transaction(level1, Decimal("200"))

    with patch("apps.commissions.tasks.timezone.now", return_value=RUN_AT):
        summary = calculate_matching_bonus()

    assert summary.get("skipped") is not True
    cache.delete(BINARY_BONUS_LOCK_KEY)


@pytest.mark.django_db
def test_interval_self_syncs_using_a_days_period_not_minutes():
    stale_schedule = IntervalSchedule.objects.create(
        every=3, period=IntervalSchedule.DAYS
    )
    task = PeriodicTask.objects.get(name=MATCHING_BONUS_TASK_NAME)
    task.interval = stale_schedule
    task.save(update_fields=["interval"])
    config.MATCHING_BONUS_INTERVAL_DAYS = 14

    with patch("apps.commissions.tasks.timezone.now", return_value=RUN_AT):
        calculate_matching_bonus()

    task.refresh_from_db()
    assert task.interval.every == 14
    assert task.interval.period == IntervalSchedule.DAYS


@pytest.mark.django_db
def test_interval_sync_ignores_a_value_below_the_one_day_floor():
    task = PeriodicTask.objects.get(name=MATCHING_BONUS_TASK_NAME)
    original_every = task.interval.every
    config.MATCHING_BONUS_INTERVAL_DAYS = 0

    with patch("apps.commissions.tasks.timezone.now", return_value=RUN_AT):
        calculate_matching_bonus()

    task.refresh_from_db()
    assert task.interval.every == original_every
