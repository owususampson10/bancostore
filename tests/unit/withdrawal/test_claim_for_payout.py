from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model

import pytest

from apps.distributors.models import Distributor
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit
from apps.withdrawal.models import WithdrawalRequest
from apps.withdrawal.services import (
    WithdrawalRequestNotFound,
    WithdrawalRequestNotPending,
    approve_withdrawal_request,
    claim_for_payout,
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


@pytest.mark.django_db
def test_claiming_an_approved_request_transitions_it_to_queued_for_payout():
    request = _make_approved_debited_request()

    claimed = claim_for_payout(request)

    assert claimed.status == WithdrawalRequest.Status.QUEUED_FOR_PAYOUT


@pytest.mark.django_db
def test_claiming_generates_a_paystack_valid_reference():
    """Paystack requires 16-50 chars, lowercase alphanumeric plus -/_
    only -- confirmed against Paystack's real docs during Task 16e."""
    request = _make_approved_debited_request()

    claimed = claim_for_payout(request)

    reference = claimed.paystack_transfer_reference
    assert reference is not None
    assert 16 <= len(reference) <= 50
    assert reference == reference.lower()
    assert all(c.isalnum() or c in "-_" for c in reference)


@pytest.mark.django_db
def test_claiming_the_same_request_twice_is_idempotent():
    """A retry (Celery redelivery, or process_withdrawal_payout calling
    this unconditionally on every attempt) must not re-claim or
    regenerate the reference -- the second call is a pure no-op."""
    request = _make_approved_debited_request()

    first = claim_for_payout(request)
    second = claim_for_payout(request)

    assert second.status == WithdrawalRequest.Status.QUEUED_FOR_PAYOUT
    assert second.paystack_transfer_reference == first.paystack_transfer_reference


@pytest.mark.django_db
def test_claiming_does_not_call_out_to_paystack():
    """doubt-driven-development finding: holding a row lock across a
    Paystack HTTP round-trip is a real DoS vector -- this function must
    never make an external call at all."""
    from unittest.mock import patch

    request = _make_approved_debited_request()

    with (
        patch("apps.distributors.paystack.requests.post") as mock_post,
        patch("apps.distributors.paystack.requests.get") as mock_get,
    ):
        claim_for_payout(request)

    mock_post.assert_not_called()
    mock_get.assert_not_called()


@pytest.mark.django_db
def test_claiming_a_request_that_does_not_exist_raises_not_found():
    request = _make_approved_debited_request()
    request.delete()

    with pytest.raises(WithdrawalRequestNotFound):
        claim_for_payout(request)


@pytest.mark.django_db
def test_claiming_a_rejected_request_raises_not_pending():
    request = _make_approved_debited_request()
    request.status = WithdrawalRequest.Status.REJECTED
    request.save(update_fields=["status"])

    with pytest.raises(WithdrawalRequestNotPending):
        claim_for_payout(request)


@pytest.mark.django_db
def test_claiming_a_paid_request_raises_not_pending():
    request = _make_approved_debited_request()
    request.status = WithdrawalRequest.Status.PAID
    request.save(update_fields=["status"])

    with pytest.raises(WithdrawalRequestNotPending):
        claim_for_payout(request)
