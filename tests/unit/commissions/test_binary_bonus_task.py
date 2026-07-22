import time
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache

import pytest
from constance import config
from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask

import apps.commissions.tasks as tasks_module
from apps.binary_tree.models import BinaryTreeEdge
from apps.commissions.models import BinaryBonusCycleFailure, BinaryBonusCycleRun
from apps.commissions.services import process_binary_bonus_for_distributor
from apps.commissions.tasks import LOCK_KEY, TASK_NAME, calculate_binary_bonus
from apps.distributors.models import Distributor
from apps.pv_ledger.models import MonthlyPersonalPv, PvDailyBucket
from apps.wallet.models import Wallet, WalletTransaction

User = get_user_model()
_phone_seq = count(1)

RUN_AT = datetime(2020, 3, 10, 10, 0, tzinfo=dt_timezone.utc)


class _FakeIdQuerySet(list):
    """distributor_ids_with_pending_pv() returns a real QuerySet, and
    calculate_binary_bonus calls .iterator() on it -- a plain list stand-in
    needs the same method to substitute cleanly in a patched test."""

    def iterator(self):
        return iter(self)


def _make_distributor():
    phone = f"+233247{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _make_pending(distributor, left=1500, right=600, period=None):
    """Gives `distributor` enough state to be both eligible for Binary
    Bonus and picked up by distributor_ids_with_pending_pv() -- monthly
    personal PV plus non-zero PV in both legs."""
    MonthlyPersonalPv.objects.create(
        distributor=distributor,
        period=(period or RUN_AT.date()).replace(day=1),
        pv=config.MIN_MONTHLY_PERSONAL_PV,
    )
    PvDailyBucket.objects.create(
        distributor=distributor,
        leg=BinaryTreeEdge.Leg.LEFT,
        date=RUN_AT.date(),
        pv=left,
    )
    PvDailyBucket.objects.create(
        distributor=distributor,
        leg=BinaryTreeEdge.Leg.RIGHT,
        date=RUN_AT.date(),
        pv=right,
    )


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_holds_one_run_at_across_the_whole_batch(mock_now):
    mock_now.return_value = RUN_AT
    d1 = _make_distributor()
    d2 = _make_distributor()
    _make_pending(d1)
    _make_pending(d2)

    calculate_binary_bonus()

    txn1 = WalletTransaction.objects.get(wallet__distributor=d1)
    txn2 = WalletTransaction.objects.get(wallet__distributor=d2)
    assert txn1.reference == f"binary-bonus-{d1.pk}-{RUN_AT.isoformat()}"
    assert txn2.reference == f"binary-bonus-{d2.pk}-{RUN_AT.isoformat()}"


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_only_iterates_distributors_with_pending_pv(mock_now):
    mock_now.return_value = RUN_AT
    with_pv = _make_distributor()
    _make_pending(with_pv)
    without_pv = _make_distributor()  # registered, but zero PV anywhere

    calculate_binary_bonus()

    assert WalletTransaction.objects.filter(wallet__distributor=with_pv).exists()
    assert not Wallet.objects.filter(distributor=without_pv).exists()


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_one_distributors_failure_does_not_abort_the_batch(mock_now):
    mock_now.return_value = RUN_AT
    failing = _make_distributor()
    _make_pending(failing)
    healthy = _make_distributor()
    _make_pending(healthy)

    real_fn = process_binary_bonus_for_distributor

    def side_effect(distributor, run_at):
        if distributor.pk == failing.pk:
            raise RuntimeError("boom")
        return real_fn(distributor, run_at)

    with patch.object(
        tasks_module, "process_binary_bonus_for_distributor", side_effect=side_effect
    ):
        summary = calculate_binary_bonus()

    assert WalletTransaction.objects.filter(wallet__distributor=healthy).exists()
    assert not Wallet.objects.filter(distributor=failing).exists()
    assert summary["failed"] == 1
    assert summary["paid"] == 1


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_a_distributor_deleted_mid_batch_does_not_abort_the_batch(mock_now):
    """distributor_ids_with_pending_pv() can return an id whose row is gone
    by the time this id is actually processed (deleted between the id list
    being fetched and the per-id lookup running) -- the batch driver must
    not fetch the row up front, and must survive a Distributor.DoesNotExist
    raised deep inside process_binary_bonus_for_distributor without aborting
    the rest of the cycle. Also proves the failure gets a durable audit
    record keyed on the bare id -- this is exactly the scenario
    BinaryBonusCycleFailure.distributor_id being a plain int rather than a
    ForeignKey exists for: a real Distributor FK would raise its own
    IntegrityError writing this row, since the referenced id never
    existed."""
    mock_now.return_value = RUN_AT
    survivor = _make_distributor()
    _make_pending(survivor)
    ghost_id = 999_999
    assert not Distributor.objects.filter(pk=ghost_id).exists()

    with patch.object(
        tasks_module,
        "distributor_ids_with_pending_pv",
        return_value=_FakeIdQuerySet([ghost_id, survivor.pk]),
    ):
        summary = calculate_binary_bonus()

    assert WalletTransaction.objects.filter(wallet__distributor=survivor).exists()
    run = BinaryBonusCycleRun.objects.get(run_at=RUN_AT)
    ghost_failure = BinaryBonusCycleFailure.objects.get(
        cycle_run=run, distributor_id=ghost_id
    )
    assert "matching query" in ghost_failure.error
    assert summary["evaluated"] == 2
    assert summary["failed"] == 1
    assert summary["paid"] == 1


@pytest.mark.django_db
def test_overlap_protection_a_concurrent_invocation_is_a_no_op():
    d = _make_distributor()
    _make_pending(d)
    assert cache.add(LOCK_KEY, "1", 60)  # simulates a cycle already in flight

    summary = calculate_binary_bonus()

    assert summary == {"skipped": True, "reason": "previous cycle still in progress"}
    assert not Wallet.objects.filter(distributor=d).exists()

    cache.delete(LOCK_KEY)


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_lock_is_released_after_a_successful_run_so_the_next_cycle_can_proceed(
    mock_now,
):
    mock_now.return_value = RUN_AT
    d = _make_distributor()
    _make_pending(d)

    calculate_binary_bonus()

    assert cache.get(LOCK_KEY) is None


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_lock_is_released_even_if_the_batch_raises(mock_now):
    mock_now.return_value = RUN_AT
    d = _make_distributor()
    _make_pending(d)

    with patch.object(
        tasks_module,
        "process_binary_bonus_for_distributor",
        side_effect=RuntimeError("boom"),
    ):
        with pytest.raises(RuntimeError):
            calculate_binary_bonus()

    assert cache.get(LOCK_KEY) is None


@pytest.mark.django_db
def test_interval_self_syncs_from_the_constance_setting():
    # The seeding migration (apps/commissions/migrations/0001_...) already
    # created this row at every=10 -- point it at a stale interval to prove
    # the task corrects it, rather than creating a competing duplicate row.
    stale_schedule = IntervalSchedule.objects.create(
        every=15, period=IntervalSchedule.MINUTES
    )
    task = PeriodicTask.objects.get(name=TASK_NAME)
    task.interval = stale_schedule
    task.save(update_fields=["interval"])
    config.BINARY_BONUS_INTERVAL_MINUTES = 20

    calculate_binary_bonus()

    task = PeriodicTask.objects.get(name=TASK_NAME)
    assert task.interval.every == 20
    assert task.interval.period == IntervalSchedule.MINUTES


@pytest.mark.django_db
def test_missing_periodic_task_row_does_not_break_the_cycle():
    """No PeriodicTask row exists (e.g. it was deleted, or this environment
    never ran the seeding migration) -- the interval-sync step must degrade
    gracefully rather than blowing up the whole cycle."""
    PeriodicTask.objects.filter(name=TASK_NAME).delete()
    d = _make_distributor()
    _make_pending(d)

    with patch("apps.commissions.tasks.timezone.now", return_value=RUN_AT):
        calculate_binary_bonus()

    assert WalletTransaction.objects.filter(wallet__distributor=d).exists()


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_every_distributor_failing_raises_instead_of_completing_silently(mock_now):
    """A systemic bug (e.g. a bad deploy breaking process_binary_bonus_for_
    distributor for everyone) must surface as a failed task run, not as a
    quiet 'nothing owed this cycle' -- those look identical to Celery/Flower
    otherwise, and this job has no human review per cycle."""
    mock_now.return_value = RUN_AT
    d1 = _make_distributor()
    _make_pending(d1)
    d2 = _make_distributor()
    _make_pending(d2)

    with patch.object(
        tasks_module,
        "process_binary_bonus_for_distributor",
        side_effect=RuntimeError("systemic bug"),
    ):
        with pytest.raises(RuntimeError):
            calculate_binary_bonus()


@pytest.mark.django_db
def test_no_pending_distributors_is_not_treated_as_total_failure():
    """evaluated == 0 must not trip the all-failed guard (0 == 0 would be a
    vacuous 'every evaluated distributor failed')."""
    summary = calculate_binary_bonus()

    assert summary["evaluated"] == 0
    assert summary["failed"] == 0


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_lock_survives_cumulative_processing_time_past_the_raw_ttl(mock_now):
    """Without per-iteration renewal, a batch whose CUMULATIVE processing
    time (across many fast distributors, not necessarily one slow one)
    exceeds LOCK_TIMEOUT_SECONDS would lose the lock mid-cycle, letting a
    second Beat trigger start a genuinely concurrent cycle with a different
    run_at. Patches the timeout down to 3 seconds, processes two
    distributors with a 1.8s pause after each (neither pause alone exceeds
    the TTL, but the pair's total does), and proves an external
    cache.add() attempt right after the second one still fails -- possible
    only because the second iteration's renewal refreshed the TTL, since
    the original lock acquired at cycle start would have already expired
    by that point on its own.

    Margins deliberately generous (3s TTL, 1.8s sleeps -- not 1s/0.6s):
    a code-review pass (2026-07-22) reproduced this test failing 3/5 times
    in isolation at the tighter margins, since the real DB/transaction cost
    of process_binary_bonus_for_distributor eats into a 1-second budget
    unpredictably. At these wider margins that variable overhead (tens of
    milliseconds locally) is negligible relative to the ~1.2s of slack on
    each side, at the cost of a slower test."""
    mock_now.return_value = RUN_AT
    d1 = _make_distributor()
    _make_pending(d1)
    d2 = _make_distributor()
    _make_pending(d2)

    real_fn = process_binary_bonus_for_distributor
    observed = {}
    calls = {"n": 0}

    def slow_then_real(distributor, run_at):
        result = real_fn(distributor, run_at)
        calls["n"] += 1
        time.sleep(1.8)
        if calls["n"] == 2:
            observed["lock_still_held"] = not cache.add(LOCK_KEY, "intruder", 60)
        return result

    with (
        patch.object(tasks_module, "LOCK_TIMEOUT_SECONDS", 3),
        patch.object(
            tasks_module,
            "process_binary_bonus_for_distributor",
            side_effect=slow_then_real,
        ),
    ):
        calculate_binary_bonus()

    assert observed["lock_still_held"] is True


@pytest.mark.django_db
def test_sync_leaves_a_crontab_scheduled_task_alone():
    """An admin who repoints this PeriodicTask at a crontab schedule via
    django_celery_beat's own admin (a legitimate, separate screen from
    constance) made a deliberate choice -- the interval-sync must not
    silently fight that by reverting it to an IntervalSchedule every
    cycle."""
    crontab = CrontabSchedule.objects.create(minute="0", hour="6-22")
    task = PeriodicTask.objects.get(name=TASK_NAME)
    task.interval = None
    task.crontab = crontab
    task.save(update_fields=["interval", "crontab"])
    config.BINARY_BONUS_INTERVAL_MINUTES = 20

    calculate_binary_bonus()

    task.refresh_from_db()
    assert task.crontab_id == crontab.pk
    assert task.interval_id is None


@pytest.mark.django_db
def test_sync_ignores_a_non_positive_interval_setting():
    task = PeriodicTask.objects.get(name=TASK_NAME)
    original_every = task.interval.every
    config.BINARY_BONUS_INTERVAL_MINUTES = 0

    calculate_binary_bonus()

    task.refresh_from_db()
    assert task.interval.every == original_every


@pytest.mark.django_db
def test_sync_ignores_a_positive_interval_below_the_enforced_floor():
    """BINARY_BONUS_INTERVAL_MINUTES has no admin-form bounds (see
    CONSTANCE_ADDITIONAL_FIELDS in apps/platform_settings/config.py) --
    without a floor here, a fat-fingered small value would sync straight
    into the real Celery Beat schedule and spam the shared worker pool
    (2026-07-22 security-and-hardening review). 1 is positive, so this
    specifically exercises the floor check, not the <= 0 guard above."""
    task = PeriodicTask.objects.get(name=TASK_NAME)
    original_every = task.interval.every
    config.BINARY_BONUS_INTERVAL_MINUTES = 1

    calculate_binary_bonus()

    task.refresh_from_db()
    assert task.interval.every == original_every


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_a_completed_cycle_persists_a_durable_audit_record(mock_now):
    """2026-07-22 security-and-hardening review: the only prior record of
    what a cycle did was ephemeral logging plus Celery's 1-day-TTL Redis
    result backend -- neither survives long enough or is queryable enough
    to answer 'what happened in the 14:10 cycle' during a support inquiry
    or incident review, for a job with no human review per cycle."""
    mock_now.return_value = RUN_AT
    paid_d = _make_distributor()
    _make_pending(paid_d)

    calculate_binary_bonus()

    run = BinaryBonusCycleRun.objects.get(run_at=RUN_AT)
    assert run.evaluated == 1
    assert run.paid == 1
    assert run.failed == 0
    assert run.total_amount == Decimal("45.00")


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_a_cycle_with_no_pending_distributors_still_persists_a_heartbeat_record(
    mock_now,
):
    """evaluated=0 is meaningful information (the job is alive and ran on
    schedule, just had nothing to do) -- worth a durable row same as any
    other outcome, not just cycles that did something."""
    mock_now.return_value = RUN_AT

    calculate_binary_bonus()

    run = BinaryBonusCycleRun.objects.get(run_at=RUN_AT)
    assert run.evaluated == 0
    assert run.paid == 0
    assert run.failed == 0
    assert run.total_amount == Decimal("0.00")


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_a_per_distributor_failure_is_recorded_with_its_error(mock_now):
    """The aggregate 'failed' count alone can't answer 'why wasn't
    distributor X paid this cycle' after the log line that explained it
    has scrolled past -- each failure needs its own durable record. A
    second, healthy distributor keeps this below the all-failed systemic
    guard, which is exercised separately below."""
    mock_now.return_value = RUN_AT
    failing = _make_distributor()
    _make_pending(failing)
    healthy = _make_distributor()
    _make_pending(healthy)

    real_fn = process_binary_bonus_for_distributor

    def side_effect(distributor, run_at):
        if distributor.pk == failing.pk:
            raise RuntimeError("simulated database blip")
        return real_fn(distributor, run_at)

    with patch.object(
        tasks_module, "process_binary_bonus_for_distributor", side_effect=side_effect
    ):
        calculate_binary_bonus()

    run = BinaryBonusCycleRun.objects.get(run_at=RUN_AT)
    failure = BinaryBonusCycleFailure.objects.get(cycle_run=run)
    assert failure.distributor_id == failing.pk
    assert "simulated database blip" in failure.error


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_routine_skips_are_not_persisted_as_failures(mock_now):
    """Ineligible / zero-weak-leg / cap-exhausted are expected, high-volume,
    routine outcomes -- already covered by DEBUG logging in
    apps/commissions/services.py. Persisting every one of those here would
    defeat the point of that existing design and bloat this table at this
    platform's stated scale, so only real per-distributor exceptions (the
    'failed' count) get a row here, not routine zero-payout skips."""
    mock_now.return_value = RUN_AT
    ineligible = _make_distributor()
    # No MonthlyPersonalPv row -- not eligible, but distributor_ids_with_
    # pending_pv() still picks it up via the PV buckets below.
    PvDailyBucket.objects.create(
        distributor=ineligible,
        leg=BinaryTreeEdge.Leg.LEFT,
        date=RUN_AT.date(),
        pv=1500,
    )
    PvDailyBucket.objects.create(
        distributor=ineligible,
        leg=BinaryTreeEdge.Leg.RIGHT,
        date=RUN_AT.date(),
        pv=600,
    )

    calculate_binary_bonus()

    run = BinaryBonusCycleRun.objects.get(run_at=RUN_AT)
    assert run.evaluated == 1
    assert run.failed == 0
    assert not BinaryBonusCycleFailure.objects.filter(cycle_run=run).exists()


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_audit_record_survives_even_when_the_cycle_raises(mock_now):
    """The systemic-failure guard raises when every evaluated distributor
    fails -- exactly the scenario most worth an audit record, since the
    Celery task result itself will show FAILURE with no further detail.
    The record must be written before that raise, not lost along with it."""
    mock_now.return_value = RUN_AT
    d = _make_distributor()
    _make_pending(d)

    with patch.object(
        tasks_module,
        "process_binary_bonus_for_distributor",
        side_effect=RuntimeError("systemic bug"),
    ):
        with pytest.raises(RuntimeError):
            calculate_binary_bonus()

    run = BinaryBonusCycleRun.objects.get(run_at=RUN_AT)
    assert run.evaluated == 1
    assert run.failed == 1
    failure = BinaryBonusCycleFailure.objects.get(cycle_run=run)
    assert "systemic bug" in failure.error


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_a_zero_rate_pauses_bonus_accrual_without_erroring(mock_now):
    """CONSTANCE_ADDITIONAL_FIELDS' percentage_field deliberately allows 0
    as an incident-response lever (see apps/platform_settings/config.py).
    This proves that lever end-to-end through the real payout path, not
    just that the admin form field accepts "0" -- a manual trace isn't a
    substitute for a test on a job that moves real money."""
    mock_now.return_value = RUN_AT
    config.BINARY_BONUS_RATE = Decimal("0")
    d = _make_distributor()
    _make_pending(d)

    summary = calculate_binary_bonus()

    assert summary["paid"] == 0
    assert summary["failed"] == 0
    assert summary["total_amount"] == "0.00"
    assert not Wallet.objects.filter(distributor=d).exists()


@pytest.mark.django_db
@patch("apps.commissions.tasks.timezone.now")
def test_a_zero_cap_pauses_payouts_without_erroring(mock_now):
    """Same lever, the other setting: WEEKLY_BINARY_BONUS_CAP=0 already
    exercised at the admin-form-field level in
    tests/unit/platform_settings/test_constance_config.py -- this proves
    it end-to-end through the real payout path instead."""
    mock_now.return_value = RUN_AT
    config.WEEKLY_BINARY_BONUS_CAP = Decimal("0")
    d = _make_distributor()
    _make_pending(d)

    summary = calculate_binary_bonus()

    assert summary["paid"] == 0
    assert summary["failed"] == 0
    assert summary["total_amount"] == "0.00"
    assert not Wallet.objects.filter(distributor=d).exists()
