from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model

import pytest

from apps.distributors.models import Distributor
from apps.distributors.paystack import PaystackNotFoundError
from apps.wallet.models import Wallet, WalletTransaction
from apps.wallet.services import credit
from apps.withdrawal.models import WithdrawalRequest
from apps.withdrawal.services import (
    approve_withdrawal_request,
    process_withdrawal_payout,
    submit_withdrawal_request,
)

User = get_user_model()
_phone_seq = count(1)


def _make_approved_debited_request(amount=Decimal("500.00")):
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
    return approve_withdrawal_request(request, reviewed_by=admin)


@patch("apps.withdrawal.services.initiate_transfer")
@patch("apps.withdrawal.services.verify_transfer")
@patch("apps.withdrawal.services.create_transfer_recipient")
@pytest.mark.django_db
def test_first_attempt_creates_recipient_and_initiates_transfer(
    mock_create_recipient, mock_verify, mock_initiate
):
    """verify_transfer raising "not found" is the expected case on a
    genuinely first attempt -- Paystack has never heard of this
    reference yet, so the fallback to initiate_transfer must fire."""
    request = _make_approved_debited_request(amount=Decimal("500.00"))
    mock_create_recipient.return_value = {"recipient_code": "RCP_abc123"}
    mock_verify.side_effect = PaystackNotFoundError("not found")
    mock_initiate.return_value = {"status": "pending"}

    process_withdrawal_payout(request)

    mock_initiate.assert_called_once()
    call_kwargs = mock_initiate.call_args.kwargs
    assert call_kwargs["amount_pesewas"] == 49500  # 495 net (5% tax on 500)
    assert call_kwargs["recipient_code"] == "RCP_abc123"
    assert call_kwargs["reference"] is not None


@patch("apps.withdrawal.services.initiate_transfer")
@patch("apps.withdrawal.services.verify_transfer")
@patch("apps.withdrawal.services.create_transfer_recipient")
@pytest.mark.django_db
def test_uses_the_local_phone_format_for_paystack_not_e164(
    mock_create_recipient, mock_verify, mock_initiate
):
    """Paystack's create_transfer_recipient was live-verified during
    Task 16e to accept and echo back the local "0..." format
    (account_number="0551234987"), not the E.164 format this codebase
    stores. This must be converted, not passed through unchanged."""
    request = _make_approved_debited_request()
    mock_create_recipient.return_value = {"recipient_code": "RCP_abc123"}
    mock_verify.side_effect = PaystackNotFoundError("not found")
    mock_initiate.return_value = {"status": "pending"}

    process_withdrawal_payout(request)

    call_kwargs = mock_create_recipient.call_args.kwargs
    assert call_kwargs["account_number"] == "0247111222"
    assert call_kwargs["bank_code"] == "MTN"
    assert call_kwargs["name"] == "Ama Mensah"


@patch("apps.withdrawal.services.initiate_transfer")
@patch("apps.withdrawal.services.verify_transfer")
@patch("apps.withdrawal.services.create_transfer_recipient")
@pytest.mark.django_db
def test_resuming_a_transfer_paystack_already_confirmed_succeeded(
    mock_create_recipient, mock_verify, mock_initiate
):
    """verify_transfer succeeding means Paystack already knows about
    this reference from an earlier attempt -- initiate_transfer must
    NOT be called again."""
    request = _make_approved_debited_request()
    mock_create_recipient.return_value = {"recipient_code": "RCP_abc123"}
    mock_verify.return_value = {"status": "success"}

    result = process_withdrawal_payout(request)

    mock_initiate.assert_not_called()
    assert result == WithdrawalRequest.Status.PAID


@patch("apps.withdrawal.services.initiate_transfer")
@patch("apps.withdrawal.services.verify_transfer")
@patch("apps.withdrawal.services.create_transfer_recipient")
@pytest.mark.django_db
def test_resuming_a_transfer_that_actually_failed_reverses_it(
    mock_create_recipient, mock_verify, mock_initiate
):
    request = _make_approved_debited_request(amount=Decimal("500.00"))
    mock_create_recipient.return_value = {"recipient_code": "RCP_abc123"}
    mock_verify.return_value = {"status": "failed"}
    wallet_before = Wallet.objects.get(distributor=request.distributor).balance

    result = process_withdrawal_payout(request)

    mock_initiate.assert_not_called()
    assert result == WithdrawalRequest.Status.PAYOUT_FAILED_REVERSED
    wallet_after = Wallet.objects.get(distributor=request.distributor).balance
    assert wallet_after == wallet_before + request.net_amount


@patch("apps.withdrawal.services.initiate_transfer")
@patch("apps.withdrawal.services.verify_transfer")
@patch("apps.withdrawal.services.create_transfer_recipient")
@pytest.mark.django_db
def test_processing_an_already_resolved_request_never_calls_paystack(
    mock_create_recipient, mock_verify, mock_initiate
):
    """A race with the webhook (or a prior batch cycle) already resolved
    this request -- process_withdrawal_payout must recognize that and
    return without making any Paystack calls at all, not error."""
    request = _make_approved_debited_request()
    request.status = WithdrawalRequest.Status.PAID
    request.save(update_fields=["status"])

    result = process_withdrawal_payout(request)

    mock_create_recipient.assert_not_called()
    mock_verify.assert_not_called()
    mock_initiate.assert_not_called()
    assert result == WithdrawalRequest.Status.PAID


@patch("apps.withdrawal.services.initiate_transfer")
@patch("apps.withdrawal.services.verify_transfer")
@patch("apps.withdrawal.services.create_transfer_recipient")
@pytest.mark.django_db
def test_recipient_code_is_cached_on_the_request(
    mock_create_recipient, mock_verify, mock_initiate
):
    request = _make_approved_debited_request()
    mock_create_recipient.return_value = {"recipient_code": "RCP_abc123"}
    mock_verify.side_effect = PaystackNotFoundError("not found")
    mock_initiate.return_value = {"status": "pending"}

    process_withdrawal_payout(request)

    request.refresh_from_db()
    assert request.paystack_recipient_code == "RCP_abc123"


@patch("apps.withdrawal.services.initiate_transfer")
@patch("apps.withdrawal.services.verify_transfer")
@patch("apps.withdrawal.services.create_transfer_recipient")
@pytest.mark.django_db
def test_a_cached_recipient_code_skips_a_redundant_paystack_call(
    mock_create_recipient, mock_verify, mock_initiate
):
    """code-review finding: create_transfer_recipient was being called
    unconditionally on every resume attempt, wasting a Paystack API call
    even when the recipient was already cached from a first attempt --
    a real cost on a request the batch driver might revisit across
    several Friday cycles while a slow transfer resolves."""
    request = _make_approved_debited_request()
    request = WithdrawalRequest.objects.get(pk=request.pk)
    request.paystack_recipient_code = "RCP_already_cached"
    request.save(update_fields=["paystack_recipient_code"])
    mock_verify.return_value = {"status": "success"}

    process_withdrawal_payout(request)

    mock_create_recipient.assert_not_called()
