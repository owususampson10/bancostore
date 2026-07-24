from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model

import pytest

from apps.distributors.models import Distributor
from apps.distributors.paystack import PaystackError, PaystackNotFoundError
from apps.wallet.models import Wallet, WalletTransaction
from apps.wallet.services import credit
from apps.withdrawal.models import WithdrawalRequest
from apps.withdrawal.services import (
    WithdrawalRequestNotFound,
    approve_withdrawal_request,
    claim_for_payout,
    submit_withdrawal_request,
)
from apps.withdrawal.tasks import process_transfer_webhook_task

User = get_user_model()
_phone_seq = count(1)


def _make_queued_for_payout_request(amount=Decimal("500.00")):
    phone = f"+233245{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor = Distributor.objects.create(
        user=user,
        phone_number=phone,
        full_name="Ama Mensah",
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
    approved = approve_withdrawal_request(request, reviewed_by=admin)
    return claim_for_payout(approved)


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.verify_transfer")
def test_success_transitions_the_request_to_paid(mock_verify):
    request = _make_queued_for_payout_request()
    mock_verify.return_value = {"status": "success"}

    process_transfer_webhook_task(request.paystack_transfer_reference)

    mock_verify.assert_called_once_with(request.paystack_transfer_reference)
    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.PAID


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.verify_transfer")
def test_failed_reverses_the_debit(mock_verify):
    request = _make_queued_for_payout_request(amount=Decimal("500.00"))
    mock_verify.return_value = {"status": "failed"}

    process_transfer_webhook_task(request.paystack_transfer_reference)

    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.PAYOUT_FAILED_REVERSED
    wallet = Wallet.objects.get(distributor=request.distributor)
    assert wallet.balance == Decimal("1000.00")  # fully restored


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.verify_transfer")
def test_a_duplicate_delivery_never_double_credits(mock_verify):
    """Paystack retries a webhook delivery until it gets a 200 -- the
    handler must be safe to run twice for the same reference, relying on
    apply_verified_transfer_outcome's own idempotency, not anything new
    here."""
    request = _make_queued_for_payout_request(amount=Decimal("500.00"))
    mock_verify.return_value = {"status": "failed"}

    process_transfer_webhook_task(request.paystack_transfer_reference)
    process_transfer_webhook_task(request.paystack_transfer_reference)

    wallet = Wallet.objects.get(distributor=request.distributor)
    assert wallet.balance == Decimal("1000.00")
    assert (
        WalletTransaction.objects.filter(
            wallet=wallet,
            transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_REVERSAL,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_an_unrecognized_reference_does_not_raise():
    process_transfer_webhook_task("withdrawal-payout-9999999999")  # no such row


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.verify_transfer")
def test_a_paystack_error_does_not_raise(mock_verify):
    request = _make_queued_for_payout_request()
    mock_verify.side_effect = PaystackError("boom")

    process_transfer_webhook_task(request.paystack_transfer_reference)  # must not raise

    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.QUEUED_FOR_PAYOUT


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.verify_transfer")
def test_a_paystack_not_found_error_does_not_raise(mock_verify):
    """PaystackNotFoundError is a subclass of PaystackError -- confirmed
    directly against apps/distributors/paystack.py before writing this
    test (doubt-driven-development, 2026-07-24), not assumed."""
    request = _make_queued_for_payout_request()
    mock_verify.side_effect = PaystackNotFoundError("not found")

    process_transfer_webhook_task(request.paystack_transfer_reference)  # must not raise

    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.QUEUED_FOR_PAYOUT


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.apply_verified_transfer_outcome")
@patch("apps.withdrawal.tasks.verify_transfer")
def test_the_request_vanishing_mid_call_does_not_raise(mock_verify, mock_apply):
    """doubt-driven-development finding, 2026-07-24: the row could be
    deleted in the window between this task's own lookup and
    apply_verified_transfer_outcome's later locked re-fetch (during the
    verify_transfer HTTP call) -- that specific exception must be caught
    here too, not just the initial lookup's."""
    request = _make_queued_for_payout_request()
    mock_verify.return_value = {"status": "success"}
    mock_apply.side_effect = WithdrawalRequestNotFound("gone")

    process_transfer_webhook_task(request.paystack_transfer_reference)  # must not raise


@pytest.mark.django_db
@patch("apps.withdrawal.tasks.verify_transfer")
def test_a_non_terminal_status_leaves_the_request_queued(mock_verify):
    request = _make_queued_for_payout_request()
    mock_verify.return_value = {"status": "pending"}

    process_transfer_webhook_task(request.paystack_transfer_reference)

    request.refresh_from_db()
    assert request.status == WithdrawalRequest.Status.QUEUED_FOR_PAYOUT
