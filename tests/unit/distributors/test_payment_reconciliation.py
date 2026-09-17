"""Task 67. The daily Paystack reconciliation: every successful payment from
the last week either became what it paid for, or is a PaymentIssue.

The webhook is the normal path, and it can be missed entirely -- the server
down for longer than Paystack retries, a misconfigured webhook URL. This is
the backstop that notices without anyone reading a log.
"""

from datetime import timedelta
from decimal import Decimal
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


def test_it_looks_back_a_week_and_leaves_the_last_hour_to_the_webhook():
    assert RECONCILIATION_WINDOW == timedelta(days=7)
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
def test_payments_that_are_not_ours_are_ignored(
    mock_list, mock_pack, mock_order, mock_reg
):
    mock_list.side_effect = _one_page(_paid("T123456789"), _paid("withdrawal-payout-1"))

    reconcile_paystack_payments()

    mock_pack.assert_not_called()
    mock_order.assert_not_called()
    mock_reg.assert_not_called()
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
def test_reconciliation_is_scheduled_daily():
    from django_celery_beat.models import PeriodicTask

    task = PeriodicTask.objects.get(name="reconcile-paystack-payments")
    assert task.task == "apps.distributors.tasks.reconcile_paystack_payments"
    assert task.enabled
    assert (task.interval.every, task.interval.period) == (1, "days")
