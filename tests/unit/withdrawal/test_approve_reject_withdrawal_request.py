import threading
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.db import connection

import pytest

from apps.distributors.models import Distributor
from apps.wallet.models import Wallet, WalletTransaction
from apps.wallet.services import credit

User = get_user_model()
_phone_seq = count(1)


def _make_eligible_distributor(balance=Decimal("1000.00")):
    phone = f"+233245{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    distributor = Distributor.objects.create(
        user=user,
        phone_number=phone,
        kyc_status=Distributor.KycStatus.APPROVED,
        mobile_money_number="+233247111222",
        mobile_money_network=Distributor.MobileMoneyNetwork.MTN,
    )
    if balance > 0:
        credit(
            distributor,
            balance,
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
            reference=f"seed-{distributor.pk}",
        )
    return distributor


def _make_admin():
    return User.objects.create_user(
        username=f"admin-{next(_phone_seq)}", password="Passw0rd!", is_staff=True
    )


def _make_submitted_request(distributor, amount=Decimal("500.00")):
    from apps.withdrawal.services import submit_withdrawal_request

    return submit_withdrawal_request(distributor, amount)


@pytest.mark.django_db
def test_approving_debits_the_wallet_for_the_net_amount():
    from apps.withdrawal.services import approve_withdrawal_request

    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    request = _make_submitted_request(distributor, Decimal("500.00"))
    admin = _make_admin()

    approve_withdrawal_request(request, reviewed_by=admin)

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("505.00")  # 1000 - 495 net (5 tax withheld)


@pytest.mark.django_db
def test_approving_sets_status_reviewer_and_timestamp():
    from apps.withdrawal.models import WithdrawalRequest
    from apps.withdrawal.services import approve_withdrawal_request

    distributor = _make_eligible_distributor()
    request = _make_submitted_request(distributor)
    admin = _make_admin()

    approved = approve_withdrawal_request(request, reviewed_by=admin)

    assert approved.status == WithdrawalRequest.Status.APPROVED_DEBITED
    assert approved.reviewed_by_id == admin.pk
    assert approved.reviewed_at is not None


@pytest.mark.django_db
def test_approving_snapshots_the_payout_destination_onto_the_request():
    from apps.withdrawal.services import approve_withdrawal_request

    distributor = _make_eligible_distributor()
    request = _make_submitted_request(distributor)
    admin = _make_admin()
    assert request.payout_mobile_money_number == ""

    approved = approve_withdrawal_request(request, reviewed_by=admin)

    assert approved.payout_mobile_money_number == "+233247111222"
    assert approved.payout_mobile_money_network == "mtn"


@pytest.mark.django_db
def test_approving_does_not_change_the_snapshot_if_distributor_edits_profile_later():
    """ADR-0004 point 7 -- the snapshot must never be re-read live off
    Distributor after approval."""
    from apps.withdrawal.services import approve_withdrawal_request

    distributor = _make_eligible_distributor()
    request = _make_submitted_request(distributor)
    admin = _make_admin()

    approved = approve_withdrawal_request(request, reviewed_by=admin)

    distributor.mobile_money_number = "+233209999999"
    distributor.mobile_money_network = Distributor.MobileMoneyNetwork.TELECEL
    distributor.save()

    approved.refresh_from_db()
    assert approved.payout_mobile_money_number == "+233247111222"
    assert approved.payout_mobile_money_network == "mtn"


@pytest.mark.django_db
def test_rejecting_never_debits_the_wallet():
    from apps.withdrawal.services import reject_withdrawal_request

    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    request = _make_submitted_request(distributor)
    admin = _make_admin()

    reject_withdrawal_request(request, reviewed_by=admin, reason="Suspicious activity")

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("1000.00")


@pytest.mark.django_db
def test_rejecting_sets_status_reviewer_reason_and_timestamp():
    from apps.withdrawal.models import WithdrawalRequest
    from apps.withdrawal.services import reject_withdrawal_request

    distributor = _make_eligible_distributor()
    request = _make_submitted_request(distributor)
    admin = _make_admin()

    rejected = reject_withdrawal_request(
        request, reviewed_by=admin, reason="Invalid mobile money number"
    )

    assert rejected.status == WithdrawalRequest.Status.REJECTED
    assert rejected.reviewed_by_id == admin.pk
    assert rejected.reviewed_at is not None
    assert rejected.rejection_reason == "Invalid mobile money number"


@pytest.mark.django_db
def test_rejecting_requires_a_non_empty_reason():
    from apps.withdrawal.services import reject_withdrawal_request

    distributor = _make_eligible_distributor()
    request = _make_submitted_request(distributor)
    admin = _make_admin()

    with pytest.raises(ValueError):
        reject_withdrawal_request(request, reviewed_by=admin, reason="")

    with pytest.raises(ValueError):
        reject_withdrawal_request(request, reviewed_by=admin, reason="   ")


@pytest.mark.django_db
def test_approving_an_already_approved_request_raises_with_the_actual_status():
    from apps.withdrawal.models import WithdrawalRequest
    from apps.withdrawal.services import (
        WithdrawalRequestNotPending,
        approve_withdrawal_request,
    )

    distributor = _make_eligible_distributor()
    request = _make_submitted_request(distributor)
    admin = _make_admin()
    approve_withdrawal_request(request, reviewed_by=admin)

    with pytest.raises(WithdrawalRequestNotPending) as excinfo:
        approve_withdrawal_request(request, reviewed_by=admin)
    assert excinfo.value.status == WithdrawalRequest.Status.APPROVED_DEBITED


@pytest.mark.django_db
def test_rejecting_an_already_rejected_request_raises_with_the_actual_status():
    from apps.withdrawal.models import WithdrawalRequest
    from apps.withdrawal.services import (
        WithdrawalRequestNotPending,
        reject_withdrawal_request,
    )

    distributor = _make_eligible_distributor()
    request = _make_submitted_request(distributor)
    admin = _make_admin()
    reject_withdrawal_request(request, reviewed_by=admin, reason="First reason")

    with pytest.raises(WithdrawalRequestNotPending) as excinfo:
        reject_withdrawal_request(request, reviewed_by=admin, reason="Second reason")
    assert excinfo.value.status == WithdrawalRequest.Status.REJECTED


@pytest.mark.django_db
def test_approving_an_already_rejected_request_raises_and_does_not_debit():
    from apps.withdrawal.services import (
        WithdrawalRequestNotPending,
        approve_withdrawal_request,
        reject_withdrawal_request,
    )

    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    request = _make_submitted_request(distributor)
    admin = _make_admin()
    reject_withdrawal_request(request, reviewed_by=admin, reason="No good")

    with pytest.raises(WithdrawalRequestNotPending):
        approve_withdrawal_request(request, reviewed_by=admin)

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("1000.00")


@pytest.mark.django_db
def test_rejecting_an_already_approved_request_raises_and_leaves_the_debit_intact():
    from apps.withdrawal.services import (
        WithdrawalRequestNotPending,
        approve_withdrawal_request,
        reject_withdrawal_request,
    )

    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    request = _make_submitted_request(distributor)
    admin = _make_admin()
    approve_withdrawal_request(request, reviewed_by=admin)

    with pytest.raises(WithdrawalRequestNotPending):
        reject_withdrawal_request(request, reviewed_by=admin, reason="Too late")

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("505.00")


@pytest.mark.django_db
def test_approving_fails_cleanly_if_kyc_was_revoked_since_submission():
    from apps.withdrawal.services import KycNotApproved, approve_withdrawal_request

    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    request = _make_submitted_request(distributor)
    admin = _make_admin()
    distributor.kyc_status = Distributor.KycStatus.PENDING
    distributor.save()

    with pytest.raises(KycNotApproved):
        approve_withdrawal_request(request, reviewed_by=admin)

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("1000.00")


@pytest.mark.django_db
def test_approving_fails_cleanly_if_payout_destination_was_cleared_since_submission():
    from apps.withdrawal.services import (
        PayoutDestinationNotSet,
        approve_withdrawal_request,
    )

    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    request = _make_submitted_request(distributor)
    admin = _make_admin()
    distributor.mobile_money_number = ""
    distributor.mobile_money_network = ""
    distributor.save()

    with pytest.raises(PayoutDestinationNotSet):
        approve_withdrawal_request(request, reviewed_by=admin)

    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("1000.00")


@pytest.mark.django_db
def test_approving_reraises_wallet_error_as_withdrawal_modules_own_exception():
    """The balance can change between submission and approval (e.g. a
    refund clawback). apps.wallet.services.InsufficientBalanceError must
    never leak through this interface -- it's re-raised as this module's
    own InsufficientWalletBalance, the same exception submit_withdrawal_
    request already uses for the equivalent submission-time check."""
    from apps.withdrawal.services import (
        InsufficientWalletBalance,
        approve_withdrawal_request,
    )

    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    request = _make_submitted_request(distributor, Decimal("500.00"))
    admin = _make_admin()
    # Drain the wallet after submission but before approval.
    wallet = Wallet.objects.get(distributor=distributor)
    wallet.balance = Decimal("10.00")
    wallet.save()

    with pytest.raises(InsufficientWalletBalance):
        approve_withdrawal_request(request, reviewed_by=admin)


@pytest.mark.django_db
def test_approving_requires_a_reviewer():
    from apps.withdrawal.services import approve_withdrawal_request

    distributor = _make_eligible_distributor()
    request = _make_submitted_request(distributor)

    with pytest.raises(TypeError):
        approve_withdrawal_request(request, reviewed_by=None)


@pytest.mark.django_db
def test_rejecting_requires_a_reviewer():
    from apps.withdrawal.services import reject_withdrawal_request

    distributor = _make_eligible_distributor()
    request = _make_submitted_request(distributor)

    with pytest.raises(TypeError):
        reject_withdrawal_request(request, reviewed_by=None, reason="Some reason")


@pytest.mark.django_db(transaction=True)
def test_concurrent_approvals_of_the_same_request_never_both_debit():
    """Mirrors the 5-thread proofs already established for wallet debit()
    and submit_withdrawal_request -- five simultaneous approval attempts
    on the same request must let exactly one succeed, the rest fail with
    WithdrawalRequestNotPending, and the wallet must be debited exactly
    once."""
    from apps.withdrawal.services import (
        WithdrawalRequestNotPending,
        approve_withdrawal_request,
    )

    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))
    request = _make_submitted_request(distributor, Decimal("500.00"))
    admin = _make_admin()
    successes = []
    not_pending_errors = []
    lock = threading.Lock()

    def attempt():
        try:
            # approve_withdrawal_request already wraps itself in
            # retry_on_lock_contention internally -- wrapping it again
            # here would double the effective retry budget and doesn't
            # match how a real caller uses it (code-review-and-quality,
            # 2026-07-23).
            approved = approve_withdrawal_request(request, reviewed_by=admin)
            with lock:
                successes.append(approved)
        except WithdrawalRequestNotPending as exc:
            with lock:
                not_pending_errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(successes) == 1
    assert len(not_pending_errors) == 4
    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("505.00")
