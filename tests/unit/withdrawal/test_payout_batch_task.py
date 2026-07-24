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

import apps.withdrawal.tasks as tasks_module
from apps.distributors.models import Distributor
from apps.distributors.paystack import PaystackNotFoundError
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit
from apps.withdrawal.models import (
    WithdrawalCycleFailure,
    WithdrawalCycleRun,
    WithdrawalRequest,
)
from apps.withdrawal.services import (
    approve_withdrawal_request,
    claim_for_payout,
    submit_withdrawal_request,
)
from apps.withdrawal.tasks import (
    WITHDRAWAL_PAYOUT_LOCK_KEY,
    WITHDRAWAL_PAYOUT_TASK_NAME,
    process_withdrawal_payouts,
)

User = get_user_model()
_phone_seq = count(1)

RUN_AT = datetime(2026, 7, 24, 0, 0, tzinfo=dt_timezone.utc)


def _make_approved_debited_request(amount=Decimal("500.00"), full_name="Ama Mensah"):
    phone = f"+233245{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor = Distributor.objects.create(
        user=user,
        phone_number=phone,
        full_name=full_name,
        kyc_status=Distributor.KycStatus.APPROVED,
        mobile_money_number="+233247111222",
        mobile_money_network=Distributor.MobileMoneyNetwork.MTN,
    )
    credit(
        distributor,
        Decimal("1000.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference=f"seed-{distributor.pk}",
    )
    request = submit_withdrawal_request(distributor, amount)
    admin = User.objects.create_user(
        username=f"admin-{next(_phone_seq)}", password="Passw0rd!", is_staff=True
    )
    return approve_withdrawal_request(request, reviewed_by=admin)


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_evaluates_both_approved_debited_and_queued_for_payout_requests(mock_now):
    mock_now.return_value = RUN_AT
    debited = _make_approved_debited_request()
    queued = claim_for_payout(_make_approved_debited_request())

    with patch.object(
        tasks_module, "process_withdrawal_payout", return_value="paid"
    ) as mock_process:
        process_withdrawal_payouts()

    processed_ids = {call.args[0].pk for call in mock_process.call_args_list}
    assert processed_ids == {debited.pk, queued.pk}


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_processes_oldest_submitted_first(mock_now):
    mock_now.return_value = RUN_AT
    first = _make_approved_debited_request()
    second = _make_approved_debited_request()

    with patch.object(
        tasks_module, "process_withdrawal_payout", return_value="paid"
    ) as mock_process:
        process_withdrawal_payouts()

    processed_order = [call.args[0].pk for call in mock_process.call_args_list]
    assert processed_order == [first.pk, second.pk]


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_a_stub_passed_to_process_withdrawal_payout_never_leaks_a_blank_field(
    mock_now,
):
    """doubt-driven-development, 2026-07-24: the batch driver passes an
    unsaved WithdrawalRequest(pk=id) stub per iteration, mirroring
    _run_commission_cycle's own Distributor(pk=id) convention. This is
    only safe because process_withdrawal_payout/claim_for_payout/
    apply_verified_transfer_outcome all re-fetch a locked row internally
    and never read any other attribute off the object passed in -- proven
    here end-to-end (real process_withdrawal_payout, only the Paystack
    calls mocked) rather than assumed from reading the source."""
    mock_now.return_value = RUN_AT
    request = _make_approved_debited_request(amount=Decimal("500.00"))

    with (
        patch("apps.withdrawal.services.create_transfer_recipient") as mock_recipient,
        patch("apps.withdrawal.services.verify_transfer") as mock_verify,
        patch("apps.withdrawal.services.initiate_transfer") as mock_initiate,
    ):
        mock_recipient.return_value = {"recipient_code": "RCP_abc123"}
        mock_verify.side_effect = PaystackNotFoundError("not found")
        mock_initiate.return_value = {"status": "pending"}

        process_withdrawal_payouts()

    mock_initiate.assert_called_once()
    call_kwargs = mock_initiate.call_args.kwargs
    assert call_kwargs["amount_pesewas"] == 49500  # 495 net (1% tax on 500)
    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.QUEUED_FOR_PAYOUT
    assert request.paystack_transfer_reference


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_one_requests_failure_does_not_abort_the_batch(mock_now):
    mock_now.return_value = RUN_AT
    failing = _make_approved_debited_request()
    _make_approved_debited_request()

    def side_effect(withdrawal_request):
        if withdrawal_request.pk == failing.pk:
            raise RuntimeError("boom")
        return "paid"

    with patch.object(
        tasks_module, "process_withdrawal_payout", side_effect=side_effect
    ):
        summary = process_withdrawal_payouts()

    assert summary["failed"] == 1
    assert summary["evaluated"] == 2


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_a_request_deleted_mid_batch_does_not_abort_the_batch(mock_now):
    """Mirrors test_a_distributor_deleted_mid_batch_does_not_abort_the_batch
    (apps/commissions): a row can vanish between the id-query snapshot and
    this id's turn in the loop. WithdrawalCycleFailure.withdrawal_request_id
    being a plain int, not a ForeignKey, is exactly what survives this --
    a real FK would itself IntegrityError writing this row."""
    mock_now.return_value = RUN_AT
    survivor = _make_approved_debited_request()
    ghost = _make_approved_debited_request()
    ghost_id = ghost.pk
    ghost.delete()

    # Real process_withdrawal_payout, unmocked -- claim_for_payout's own
    # WithdrawalRequest.DoesNotExist -> WithdrawalRequestNotFound path
    # fires naturally for the ghost id. _withdrawal_payout_ids_query is
    # patched only because a real query executed after the delete would
    # never include ghost_id in the first place -- this simulates the id
    # having been captured in the snapshot before the row vanished. The
    # survivor still goes through the real function too, so its Paystack
    # calls are mocked (never hit a live provider in tests).
    with (
        patch.object(
            tasks_module,
            "_withdrawal_payout_ids_query",
            return_value=iter([ghost_id, survivor.pk]),
        ),
        patch("apps.withdrawal.services.create_transfer_recipient") as mock_recipient,
        patch("apps.withdrawal.services.verify_transfer") as mock_verify,
        patch("apps.withdrawal.services.initiate_transfer") as mock_initiate,
    ):
        mock_recipient.return_value = {"recipient_code": "RCP_abc123"}
        mock_verify.side_effect = PaystackNotFoundError("not found")
        mock_initiate.return_value = {"status": "pending"}
        summary = process_withdrawal_payouts()

    run = WithdrawalCycleRun.objects.get(run_at=RUN_AT)
    failure = WithdrawalCycleFailure.objects.get(
        cycle_run=run, withdrawal_request_id=ghost_id
    )
    assert failure.distributor_id is None
    assert failure.net_amount is None
    assert "does not exist" in failure.error
    assert summary["evaluated"] == 2
    assert summary["failed"] == 1


@pytest.mark.django_db
def test_overlap_protection_a_concurrent_invocation_is_a_no_op():
    _make_approved_debited_request()
    assert cache.add(WITHDRAWAL_PAYOUT_LOCK_KEY, "1", 60)

    summary = process_withdrawal_payouts()

    assert summary == {"skipped": True, "reason": "previous cycle still in progress"}

    cache.delete(WITHDRAWAL_PAYOUT_LOCK_KEY)


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_lock_is_released_after_a_successful_run(mock_now):
    mock_now.return_value = RUN_AT
    _make_approved_debited_request()

    with patch.object(tasks_module, "process_withdrawal_payout", return_value="paid"):
        process_withdrawal_payouts()

    assert cache.get(WITHDRAWAL_PAYOUT_LOCK_KEY) is None


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_lock_is_released_even_if_the_batch_raises(mock_now):
    mock_now.return_value = RUN_AT
    _make_approved_debited_request()

    with patch.object(
        tasks_module, "process_withdrawal_payout", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError):
            process_withdrawal_payouts()

    assert cache.get(WITHDRAWAL_PAYOUT_LOCK_KEY) is None


@pytest.mark.django_db
def test_crontab_self_syncs_from_the_constance_setting():
    # The seeding migration already created this row at day_of_week="5"
    # (Friday) -- point it at a stale day to prove the task corrects it.
    stale_schedule = CrontabSchedule.objects.create(
        minute="0", hour="0", day_of_month="*", month_of_year="*", day_of_week="1"
    )
    task = PeriodicTask.objects.get(name=WITHDRAWAL_PAYOUT_TASK_NAME)
    task.crontab = stale_schedule
    task.save(update_fields=["crontab"])
    config.WITHDRAWAL_DAY = "saturday"

    with patch.object(tasks_module, "process_withdrawal_payout", return_value="paid"):
        process_withdrawal_payouts()

    task.refresh_from_db()
    assert task.crontab.day_of_week == "6"


@pytest.mark.django_db
def test_sync_rejects_an_unrecognized_weekday_and_leaves_existing_schedule():
    """Confirmed directly against the installed django_celery_beat package
    (doubt-driven-development, 2026-07-24): day_of_week_validator rejects
    "friday" outright, and Django model validators never run automatically
    on .save()/get_or_create() -- this must be checked explicitly before
    writing, not left to fail deep inside Beat's own scheduling loop."""
    task = PeriodicTask.objects.get(name=WITHDRAWAL_PAYOUT_TASK_NAME)
    original_day_of_week = task.crontab.day_of_week
    config.WITHDRAWAL_DAY = "someday"

    with patch.object(tasks_module, "process_withdrawal_payout", return_value="paid"):
        process_withdrawal_payouts()

    task.refresh_from_db()
    assert task.crontab.day_of_week == original_day_of_week


@pytest.mark.django_db
def test_sync_leaves_an_interval_scheduled_task_alone():
    """An admin who repoints this PeriodicTask at an interval schedule via
    django_celery_beat's own admin made a deliberate choice -- the
    crontab-sync must not silently fight that."""
    interval = IntervalSchedule.objects.create(every=5, period=IntervalSchedule.DAYS)
    task = PeriodicTask.objects.get(name=WITHDRAWAL_PAYOUT_TASK_NAME)
    task.crontab = None
    task.interval = interval
    task.save(update_fields=["crontab", "interval"])
    config.WITHDRAWAL_DAY = "monday"

    with patch.object(tasks_module, "process_withdrawal_payout", return_value="paid"):
        process_withdrawal_payouts()

    task.refresh_from_db()
    assert task.interval_id == interval.pk
    assert task.crontab_id is None


@pytest.mark.django_db
def test_missing_periodic_task_row_does_not_break_the_cycle():
    PeriodicTask.objects.filter(name=WITHDRAWAL_PAYOUT_TASK_NAME).delete()

    with patch.object(tasks_module, "process_withdrawal_payout", return_value="paid"):
        summary = process_withdrawal_payouts()

    assert summary["evaluated"] == 0


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_a_completed_cycle_persists_a_durable_audit_record(mock_now):
    mock_now.return_value = RUN_AT
    request = _make_approved_debited_request(amount=Decimal("500.00"))

    def side_effect(withdrawal_request):
        WithdrawalRequest.objects.filter(pk=withdrawal_request.pk).update(
            status=WithdrawalRequest.Status.PAID
        )
        return WithdrawalRequest.Status.PAID

    with patch.object(
        tasks_module, "process_withdrawal_payout", side_effect=side_effect
    ):
        process_withdrawal_payouts()

    run = WithdrawalCycleRun.objects.get(run_at=RUN_AT)
    assert run.evaluated == 1
    assert run.paid == 1
    assert run.failed == 0
    assert run.total_amount == request.net_amount


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_a_cycle_with_no_pending_requests_still_persists_a_heartbeat_record(mock_now):
    mock_now.return_value = RUN_AT

    process_withdrawal_payouts()

    run = WithdrawalCycleRun.objects.get(run_at=RUN_AT)
    assert run.evaluated == 0
    assert run.paid == 0
    assert run.failed == 0
    assert run.total_amount == Decimal("0.00")


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_a_per_request_failure_is_recorded_with_distributor_id_and_net_amount(
    mock_now,
):
    """doubt-driven-development, 2026-07-24: unlike a failed commission
    calculation (no money ever moved), a failed withdrawal payout means
    real money is already debited and stuck -- the failure record must
    be able to say whose money it was and how much, not just an id."""
    mock_now.return_value = RUN_AT
    failing = _make_approved_debited_request(amount=Decimal("500.00"))
    _make_approved_debited_request()

    def side_effect(withdrawal_request):
        if withdrawal_request.pk == failing.pk:
            raise RuntimeError("simulated Paystack outage")
        return "paid"

    with patch.object(
        tasks_module, "process_withdrawal_payout", side_effect=side_effect
    ):
        process_withdrawal_payouts()

    run = WithdrawalCycleRun.objects.get(run_at=RUN_AT)
    failure = WithdrawalCycleFailure.objects.get(
        cycle_run=run, withdrawal_request_id=failing.pk
    )
    assert failure.distributor_id == failing.distributor_id
    assert failure.net_amount == failing.net_amount
    assert "simulated Paystack outage" in failure.error


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_audit_record_survives_even_when_the_cycle_raises(mock_now):
    mock_now.return_value = RUN_AT
    _make_approved_debited_request()

    with patch.object(
        tasks_module, "process_withdrawal_payout", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError):
            process_withdrawal_payouts()

    assert WithdrawalCycleRun.objects.filter(run_at=RUN_AT).exists()


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_every_request_failing_raises_instead_of_completing_silently(mock_now):
    mock_now.return_value = RUN_AT
    _make_approved_debited_request()
    _make_approved_debited_request()

    with patch.object(
        tasks_module, "process_withdrawal_payout", side_effect=RuntimeError("boom")
    ):
        with pytest.raises(RuntimeError, match="every one of 2"):
            process_withdrawal_payouts()


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_no_pending_requests_is_not_treated_as_total_failure(mock_now):
    mock_now.return_value = RUN_AT

    summary = process_withdrawal_payouts()

    assert summary["evaluated"] == 0
    assert summary["failed"] == 0


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.timezone.now")
def test_paid_count_reflects_actual_db_state_not_the_return_value(mock_now):
    """doubt-driven-development, 2026-07-24: process_withdrawal_payout's
    return value can't distinguish "this call just paid the row" from
    "already resolved by something else" -- so paid/total_amount must be
    derived from an end-of-cycle DB aggregate, never accumulated from the
    per-row return string. Here the mock LIES (returns "paid" without
    actually changing the row's status) to prove the driver isn't fooled:
    a buggy implementation trusting the return value would report paid=1;
    the correct one reports paid=0, since the DB truthfully still shows
    approved_debited."""
    mock_now.return_value = RUN_AT
    _make_approved_debited_request()

    with patch.object(tasks_module, "process_withdrawal_payout", return_value="paid"):
        summary = process_withdrawal_payouts()

    assert summary["paid"] == 0
    assert summary["total_amount"] == "0.00"
    run = WithdrawalCycleRun.objects.get(run_at=RUN_AT)
    assert run.paid == 0
