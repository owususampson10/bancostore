import threading
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.db import connection

import pytest

from apps.distributors.models import Distributor
from apps.wallet.models import Wallet, WalletTransaction
from apps.wallet.services import credit
from apps.withdrawal.models import WithdrawalRequest
from apps.withdrawal.services import (
    WithdrawalRequestNotFound,
    apply_verified_transfer_outcome,
    approve_withdrawal_request,
    claim_for_payout,
    submit_withdrawal_request,
)

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
def test_success_transitions_to_paid_without_touching_the_wallet():
    request = _make_queued_for_payout_request()
    wallet_before = Wallet.objects.get(distributor=request.distributor).balance

    result = apply_verified_transfer_outcome(request, "success")

    assert result.status == WithdrawalRequest.Status.PAID
    wallet_after = Wallet.objects.get(distributor=request.distributor).balance
    assert wallet_after == wallet_before


@pytest.mark.django_db
def test_failed_credits_back_net_amount_and_marks_reversed():
    request = _make_queued_for_payout_request(amount=Decimal("500.00"))
    wallet_before = Wallet.objects.get(distributor=request.distributor).balance

    result = apply_verified_transfer_outcome(request, "failed")

    assert result.status == WithdrawalRequest.Status.PAYOUT_FAILED_REVERSED
    wallet_after = Wallet.objects.get(distributor=request.distributor).balance
    assert wallet_after == wallet_before + request.net_amount


@pytest.mark.django_db
def test_reversed_credits_back_net_amount_and_marks_reversed():
    request = _make_queued_for_payout_request(amount=Decimal("500.00"))
    wallet_before = Wallet.objects.get(distributor=request.distributor).balance

    result = apply_verified_transfer_outcome(request, "reversed")

    assert result.status == WithdrawalRequest.Status.PAYOUT_FAILED_REVERSED
    wallet_after = Wallet.objects.get(distributor=request.distributor).balance
    assert wallet_after == wallet_before + request.net_amount


@pytest.mark.django_db
def test_failed_uses_the_withdrawal_reversal_transaction_type():
    request = _make_queued_for_payout_request()

    apply_verified_transfer_outcome(request, "failed")

    wallet = Wallet.objects.get(distributor=request.distributor)
    reversal_tx = wallet.transactions.get(
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_REVERSAL
    )
    assert reversal_tx.amount == request.net_amount


@pytest.mark.django_db
def test_a_pending_verified_status_is_a_no_op():
    request = _make_queued_for_payout_request()

    result = apply_verified_transfer_outcome(request, "pending")

    assert result.status == WithdrawalRequest.Status.QUEUED_FOR_PAYOUT


@pytest.mark.django_db
def test_applying_success_twice_is_idempotent():
    """Simulates a duplicate webhook delivery for the same event."""
    request = _make_queued_for_payout_request()

    apply_verified_transfer_outcome(request, "success")
    result = apply_verified_transfer_outcome(request, "success")

    assert result.status == WithdrawalRequest.Status.PAID


@pytest.mark.django_db
def test_applying_failed_twice_never_double_credits():
    """The critical race this function exists to close: a duplicate
    webhook delivery, or a webhook racing the batch driver's own
    verify_transfer resume path, must never credit the distributor
    twice for the same failed transfer."""
    request = _make_queued_for_payout_request(amount=Decimal("500.00"))
    wallet_before = Wallet.objects.get(distributor=request.distributor).balance

    apply_verified_transfer_outcome(request, "failed")
    apply_verified_transfer_outcome(request, "failed")

    wallet_after = Wallet.objects.get(distributor=request.distributor).balance
    assert wallet_after == wallet_before + request.net_amount  # only once

    wallet = Wallet.objects.get(distributor=request.distributor)
    reversal_count = wallet.transactions.filter(
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_REVERSAL
    ).count()
    assert reversal_count == 1


@pytest.mark.django_db
def test_applying_an_outcome_to_a_nonexistent_request_raises_not_found():
    request = _make_queued_for_payout_request()
    request.delete()

    with pytest.raises(WithdrawalRequestNotFound):
        apply_verified_transfer_outcome(request, "success")


@pytest.mark.django_db(transaction=True)
def test_concurrent_applications_of_a_failed_outcome_never_double_reverse():
    """Mirrors the 5-thread proofs already established for wallet
    debit() and approve_withdrawal_request's own concurrent-approval
    test -- this is the exact race all three doubt-driven-development
    reviews (single-model, Gemini, ChatGPT) converged on independently:
    a webhook worker and the batch driver's own verify_transfer resume
    path could both apply the same failed outcome to the same request
    at nearly the same instant. Sequential double-call tests elsewhere
    in this file prove logical idempotency; this proves it holds under
    genuine concurrent access, not just in series."""
    request = _make_queued_for_payout_request(amount=Decimal("500.00"))
    wallet_before = Wallet.objects.get(distributor=request.distributor).balance
    lock = threading.Lock()
    results = []

    def attempt():
        try:
            result = apply_verified_transfer_outcome(request, "failed")
            with lock:
                results.append(result.status)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Idempotent-no-op by design (unlike approve_withdrawal_request,
    # which raises for every attempt after the first) -- every one of
    # the 5 concurrent calls returns normally with the same final status.
    assert results == [WithdrawalRequest.Status.PAYOUT_FAILED_REVERSED] * 5

    wallet_after = Wallet.objects.get(distributor=request.distributor).balance
    assert wallet_after == wallet_before + request.net_amount  # credited exactly once

    wallet = Wallet.objects.get(distributor=request.distributor)
    reversal_count = wallet.transactions.filter(
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_REVERSAL
    ).count()
    assert reversal_count == 1
