"""Task 67. The daily Paystack reconciliation: every successful payment from
the last week either became what it paid for, or is a PaymentIssue.

The webhook is the normal path, and it can be missed entirely -- the server
down for longer than Paystack retries, a misconfigured webhook URL. This is
the backstop that notices without anyone reading a log.
"""

from datetime import timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone

import pytest

from apps.distributors.models import Distributor, PaymentIssue, PendingRegistration
from apps.distributors.paystack import PaystackError
from apps.distributors.tasks import (
    RECONCILIATION_GRACE,
    RECONCILIATION_WINDOW,
    reconcile_paystack_payments,
)
from apps.orders.models import Order

User = get_user_model()
_seq = count(1)

LIST = "apps.distributors.tasks.list_transactions"
VERIFY = "apps.distributors.tasks.verify_transaction"
CONSUME_REG = "apps.distributors.tasks.consume_paid_registration"
CONSUME_PACK = "apps.distributors.tasks.consume_paid_starter_pack"
CONFIRM_ORDER = "apps.distributors.tasks.confirm_order_payment"


@pytest.fixture(autouse=True)
def _run_on_commit_now(monkeypatch):
    monkeypatch.setattr(
        "apps.distributors.payment_issues.transaction.on_commit", lambda fn: fn()
    )


def _paid(reference, *, hours_ago=3, amount=10000):
    return {
        "reference": reference,
        "status": "success",
        "amount": amount,
        "paid_at": (timezone.now() - timedelta(hours=hours_ago)).isoformat(),
        "customer": {"email": "payer@example.test"},
    }


def _one_page(*transactions):
    return lambda **kwargs: (list(transactions), {"page": 1, "pageCount": 1})


def _make_distributor(phone="+233209999999", **fields):
    user = User.objects.create_user(username=phone, password="x")
    return Distributor.objects.create(user=user, phone_number=phone, **fields)


def _make_order(reference, **fields):
    return Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=Decimal("100.00"),
        delivery_fee=Decimal("0"),
        discount_amount=Decimal("0"),
        total=Decimal("100.00"),
        payment_reference=reference,
        **fields,
    )


def test_it_leaves_the_last_hour_to_the_webhook():
    assert RECONCILIATION_GRACE == timedelta(hours=1)


# --- Paging ----------------------------------------------------------------------


@pytest.mark.django_db
@patch(CONSUME_REG)
@patch(LIST)
def test_it_reads_every_page_from_a_week_ago(mock_list, mock_consume):
    pages = {
        1: ([_paid("reg-a")], {"page": 1, "pageCount": 2}),
        2: ([_paid("reg-b")], {"page": 2, "pageCount": 2}),
    }
    mock_list.side_effect = lambda **kwargs: pages[kwargs["page"]]

    reconcile_paystack_payments()

    assert [c.kwargs["page"] for c in mock_list.call_args_list] == [1, 2]
    since = mock_list.call_args_list[0].kwargs["from_"]
    assert timezone.now() - since >= RECONCILIATION_WINDOW
    assert mock_list.call_args_list[0].kwargs["status"] == "success"
    assert [c.args[0] for c in mock_consume.call_args_list] == ["reg-a", "reg-b"]


@pytest.mark.django_db
@patch(LIST)
def test_a_listing_failure_raises_so_the_run_is_marked_failed(mock_list):
    mock_list.side_effect = PaystackError("down")

    with pytest.raises(PaystackError):
        reconcile_paystack_payments()


@pytest.mark.django_db
@patch(CONSUME_REG)
@patch(LIST)
def test_payments_from_the_last_hour_are_left_to_the_webhook(mock_list, mock_consume):
    mock_list.side_effect = _one_page(_paid("reg-new", hours_ago=0))

    reconcile_paystack_payments()

    mock_consume.assert_not_called()


@pytest.mark.django_db
@patch(CONSUME_REG)
@patch(LIST)
def test_a_payment_already_flagged_is_not_looked_at_again(mock_list, mock_consume):
    PaymentIssue.objects.create(
        reference="reg-a", kind=PaymentIssue.Kind.REGISTRATION_UNMATCHED
    )
    mock_list.side_effect = _one_page(_paid("reg-a"))

    reconcile_paystack_payments()

    mock_consume.assert_not_called()


@pytest.mark.django_db
@patch(CONSUME_REG)
@patch(LIST)
def test_one_failing_payment_does_not_stop_the_rest(mock_list, mock_consume):
    mock_list.side_effect = _one_page(_paid("reg-a"), _paid("reg-b"))
    mock_consume.side_effect = [RuntimeError("boom"), None]

    summary = reconcile_paystack_payments()

    assert mock_consume.call_count == 2
    assert summary["failed"] == 1


# --- Registrations -----------------------------------------------------------------


@pytest.mark.django_db
@patch(CONSUME_REG)
@patch(LIST)
def test_a_registration_that_created_its_account_is_left_alone(mock_list, mock_consume):
    PendingRegistration.objects.create(
        full_name="Kofi",
        phone_number="+233241111111",
        email="",
        address="a",
        area="b",
        password_hash="x",
        sponsor=_make_distributor(),
        consumed_at=timezone.now(),
        consumed_reference="reg-a",
    )
    mock_list.side_effect = _one_page(_paid("reg-a"))

    reconcile_paystack_payments()

    mock_consume.assert_not_called()


@pytest.mark.django_db
@patch(CONSUME_REG)
@patch(LIST)
def test_any_other_registration_payment_goes_through_consume(mock_list, mock_consume):
    """consume_paid_registration creates the account or records the issue
    itself -- including the 2026-09-16 case, where the registration was gone."""
    mock_list.side_effect = _one_page(_paid("reg-a"))

    reconcile_paystack_payments()

    mock_consume.assert_called_once_with("reg-a")


# --- Starter packs -----------------------------------------------------------------


@pytest.mark.django_db
@patch(CONSUME_PACK)
@patch(LIST)
def test_an_applied_starter_pack_is_left_alone(mock_list, mock_consume):
    _make_distributor(
        starter_pack_payment_reference="pack-1-aa",
        starter_pack_confirmed_at=timezone.now(),
    )
    mock_list.side_effect = _one_page(_paid("pack-1-aa"))

    reconcile_paystack_payments()

    mock_consume.assert_not_called()
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch(CONSUME_PACK)
@patch(LIST)
def test_a_pack_refunded_by_cooling_off_is_not_flagged(mock_list, mock_consume):
    """Cooling-off clears starter_pack_confirmed_at, and the refund already
    went to the distributor's wallet."""
    _make_distributor(
        starter_pack_payment_reference="pack-1-aa",
        cooling_off_cancelled_at=timezone.now(),
    )
    mock_list.side_effect = _one_page(_paid("pack-1-aa"))

    reconcile_paystack_payments()

    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch(VERIFY)
@patch(CONSUME_PACK)
@patch(LIST)
def test_a_missed_starter_pack_webhook_is_applied_without_an_issue(
    mock_list, mock_consume, mock_verify
):
    distributor = _make_distributor(starter_pack_payment_reference="pack-1-aa")
    mock_list.side_effect = _one_page(_paid("pack-1-aa"))
    mock_consume.side_effect = lambda ref: Distributor.objects.filter(
        pk=distributor.pk
    ).update(starter_pack_confirmed_at=timezone.now())

    reconcile_paystack_payments()

    mock_consume.assert_called_once_with("pack-1-aa")
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch(VERIFY)
@patch(CONSUME_PACK)
@patch(LIST)
def test_a_paid_starter_pack_that_will_not_apply_is_flagged(
    mock_list, mock_consume, mock_verify
):
    """Checked with Paystack a second time first, and tried once more."""
    _make_distributor(starter_pack_payment_reference="pack-1-aa")
    mock_list.side_effect = _one_page(_paid("pack-1-aa"))
    mock_verify.return_value = _paid("pack-1-aa")

    reconcile_paystack_payments()

    assert mock_consume.call_count == 2
    issue = PaymentIssue.objects.get(reference="pack-1-aa")
    assert issue.kind == PaymentIssue.Kind.STARTER_PACK_NOT_APPLIED


@pytest.mark.django_db
@patch(VERIFY)
@patch(CONSUME_PACK)
@patch(LIST)
def test_a_verify_timeout_never_becomes_a_refund_alert(
    mock_list, mock_consume, mock_verify
):
    """consume_* swallows a Paystack timeout, so "still not applied" alone
    proves nothing. A false alert would get a real purchase refunded."""
    _make_distributor(starter_pack_payment_reference="pack-1-aa")
    mock_list.side_effect = _one_page(_paid("pack-1-aa"))
    mock_verify.side_effect = PaystackError("timed out")

    reconcile_paystack_payments()

    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch(VERIFY)
@patch(CONSUME_PACK)
@patch(LIST)
def test_a_starter_pack_payment_with_no_distributor_is_flagged(
    mock_list, mock_consume, mock_verify
):
    mock_list.side_effect = _one_page(_paid("pack-99-zz"))
    mock_verify.return_value = _paid("pack-99-zz")

    reconcile_paystack_payments()

    assert (
        PaymentIssue.objects.get(reference="pack-99-zz").kind
        == PaymentIssue.Kind.STARTER_PACK_NOT_APPLIED
    )


# --- Orders ------------------------------------------------------------------------


@pytest.mark.django_db
@patch(CONFIRM_ORDER)
@patch(LIST)
def test_a_confirmed_order_is_left_alone_even_if_later_cancelled(
    mock_list, mock_confirm
):
    _make_order("order-1", status=Order.Status.CANCELLED, confirmed_at=timezone.now())
    mock_list.side_effect = _one_page(_paid("order-1"))

    reconcile_paystack_payments()

    mock_confirm.assert_not_called()
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch(VERIFY)
@patch(CONFIRM_ORDER)
@patch(LIST)
def test_a_paid_order_cancelled_before_it_was_confirmed_is_flagged(
    mock_list, mock_confirm, mock_verify
):
    """The same failure as the registration incident, for orders: auto-cancel
    closes an unpaid order, then the customer pays on the still-open page."""
    _make_order("order-1", status=Order.Status.CANCELLED)
    mock_list.side_effect = _one_page(_paid("order-1"))
    mock_verify.return_value = _paid("order-1")

    reconcile_paystack_payments()

    assert (
        PaymentIssue.objects.get(reference="order-1").kind
        == PaymentIssue.Kind.ORDER_NOT_APPLIED
    )


@pytest.mark.django_db
@patch(VERIFY)
@patch(CONFIRM_ORDER)
@patch(LIST)
def test_a_missed_order_webhook_is_confirmed_without_an_issue(
    mock_list, mock_confirm, mock_verify
):
    order = _make_order("order-1")
    mock_list.side_effect = _one_page(_paid("order-1"))
    mock_confirm.side_effect = lambda ref: Order.objects.filter(pk=order.pk).update(
        status=Order.Status.CONFIRMED, confirmed_at=timezone.now()
    )

    reconcile_paystack_payments()

    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch(CONSUME_REG)
@patch(CONFIRM_ORDER)
@patch(CONSUME_PACK)
@patch(LIST)
def test_a_payment_that_is_not_ours_goes_through_none_of_the_three_paths(
    mock_list, mock_pack, mock_order, mock_reg
):
    """Task 68g changed what happens next: it used to be skipped in silence
    (the old assertion here was `not PaymentIssue.objects.exists()`), which
    reads exactly like "nothing happened". It is now recorded -- see
    test_a_payment_from_outside_bancostore_is_recorded_not_ignored."""
    mock_list.side_effect = _one_page(_paid("T123456789"), _paid("withdrawal-payout-1"))

    reconcile_paystack_payments()

    mock_pack.assert_not_called()
    mock_order.assert_not_called()
    mock_reg.assert_not_called()
    assert not PaymentIssue.objects.filter(reference="withdrawal-payout-1").exists()


@pytest.mark.django_db
def test_reconciliation_is_scheduled_daily():
    from django_celery_beat.models import PeriodicTask

    task = PeriodicTask.objects.get(name="reconcile-paystack-payments")
    assert task.task == "apps.distributors.tasks.reconcile_paystack_payments"
    assert task.enabled
    assert (task.interval.every, task.interval.period) == (1, "days")


# --- Task 68g/68h: nothing successful is ignored ------------------------------


def test_the_window_covers_a_late_payment():
    """Settled by live experiment 2026-09-18: Paystack's `from` filter is on
    CREATED time, so a 7-day window could not see a checkout opened 8 days
    ago and paid today -- the very case this job exists to catch."""
    assert RECONCILIATION_WINDOW == timedelta(days=30)


@pytest.mark.django_db
@patch(LIST)
def test_a_payment_from_outside_bancostore_is_recorded_not_ignored(mock_list):
    mock_list.side_effect = _one_page(_paid("T123456789"))

    reconcile_paystack_payments()

    issue = PaymentIssue.objects.get(reference="T123456789")
    assert issue.kind == PaymentIssue.Kind.UNRECOGNISED_PAYMENT


@pytest.mark.django_db
@patch(LIST)
def test_a_transfer_reference_is_left_to_the_payout_job(mock_list):
    """Money going out, not a checkout nobody claimed."""
    mock_list.side_effect = _one_page(_paid("withdrawal-payout-000042"))

    reconcile_paystack_payments()

    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.tasks.send_admin_notification")
@patch(LIST)
def test_hitting_the_page_cap_alerts_instead_of_only_logging(mock_list, mock_bell):
    from apps.distributors.tasks import RECONCILIATION_MAX_PAGES

    mock_list.return_value = (
        [],
        {"page": 1, "pageCount": RECONCILIATION_MAX_PAGES + 5},
    )
    mock_list.side_effect = lambda **kwargs: (
        [_paid(f"reg-{kwargs['page']}")],
        {"page": kwargs["page"], "pageCount": RECONCILIATION_MAX_PAGES + 5},
    )

    with patch(CONSUME_REG):
        reconcile_paystack_payments()

    assert mock_list.call_count == RECONCILIATION_MAX_PAGES
    mock_bell.assert_called_once()
    assert "reconciliation stopped" in mock_bell.call_args.args[1].lower()


# --- Task 68i: a withdrawal cannot be debited and silently never paid ---------


def _make_queued_withdrawal(*, days_old, net_amount=Decimal("495.00")):
    from apps.withdrawal.models import WithdrawalRequest

    distributor = _make_distributor(phone=f"+2332099{next(_seq):05d}")
    request = WithdrawalRequest.objects.create(
        distributor=distributor,
        amount=Decimal("500.00"),
        tax_amount=Decimal("5.00"),
        net_amount=net_amount,
        status=WithdrawalRequest.Status.QUEUED_FOR_PAYOUT,
    )
    WithdrawalRequest.objects.filter(pk=request.pk).update(
        created_at=timezone.now() - timedelta(days=days_old)
    )
    return request


@pytest.mark.django_db
@patch(LIST)
def test_a_payout_stuck_for_days_is_reported(mock_list):
    """The wallet was debited at approval; only "failed"/"reversed" put it
    back. Paystack's other statuses (otp, abandoned, blocked, rejected)
    leave the money gone with nobody told."""
    mock_list.side_effect = _one_page()
    request = _make_queued_withdrawal(days_old=3)

    reconcile_paystack_payments()

    issue = PaymentIssue.objects.get(reference=f"withdrawal-payout-{request.pk:010d}")
    assert issue.kind == PaymentIssue.Kind.WITHDRAWAL_NOT_PAID
    assert "495" in issue.detail


@pytest.mark.django_db
@patch(LIST)
def test_a_payout_queued_today_is_left_alone(mock_list):
    mock_list.side_effect = _one_page()
    _make_queued_withdrawal(days_old=0)

    reconcile_paystack_payments()

    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch(LIST)
def test_a_stuck_payout_is_reported_once_not_daily(mock_list):
    mock_list.side_effect = _one_page()
    _make_queued_withdrawal(days_old=5)

    reconcile_paystack_payments()
    reconcile_paystack_payments()

    assert PaymentIssue.objects.count() == 1


@pytest.mark.django_db
@patch(LIST)
def test_the_wallet_is_never_credited_back_automatically(mock_list):
    """An unknown Paystack status can still become a real transfer later;
    crediting the wallet for one that then completes would pay twice. A
    human decides -- this only makes sure a human knows."""
    from apps.wallet.models import WalletTransaction

    mock_list.side_effect = _one_page()
    _make_queued_withdrawal(days_old=3)

    reconcile_paystack_payments()

    assert not WalletTransaction.objects.filter(
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_REVERSAL
    ).exists()
