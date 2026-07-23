import threading
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.db import connection

import pytest

from apps.distributors.models import Distributor
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit
from bancostore.concurrency import retry_on_lock_contention

User = get_user_model()
_phone_seq = count(1)


def _make_eligible_distributor(balance=Decimal("1000.00")):
    """KYC approved, payout destination set, wallet funded -- the baseline
    "everything is fine" distributor each rejection test then breaks one
    thing on, and the happy-path tests use directly."""
    phone = f"+233244{next(_phone_seq):06d}"
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


@pytest.mark.django_db
def test_matches_the_documented_worked_example_exactly():
    """GHS 500 requested -> GHS 5 tax (1% WITHHOLDING_TAX_RATE default) ->
    GHS 495 net -- the exact worked example this task's own spec cites."""
    from apps.withdrawal.services import submit_withdrawal_request

    distributor = _make_eligible_distributor()

    request = submit_withdrawal_request(distributor, Decimal("500.00"))

    assert request.amount == Decimal("500.00")
    assert request.tax_amount == Decimal("5.00")
    assert request.net_amount == Decimal("495.00")


@pytest.mark.django_db
def test_creates_request_at_submitted_status():
    from apps.withdrawal.models import WithdrawalRequest
    from apps.withdrawal.services import submit_withdrawal_request

    distributor = _make_eligible_distributor()

    request = submit_withdrawal_request(distributor, Decimal("500.00"))

    assert request.status == WithdrawalRequest.Status.SUBMITTED


@pytest.mark.django_db
def test_does_not_touch_the_wallet():
    """ADR-0004 decision 3 -- debit happens at admin approval (16d), not
    submission. Submitting must never change the balance."""
    from apps.withdrawal.services import submit_withdrawal_request

    distributor = _make_eligible_distributor(balance=Decimal("1000.00"))

    submit_withdrawal_request(distributor, Decimal("500.00"))

    distributor.wallet.refresh_from_db()
    assert distributor.wallet.balance == Decimal("1000.00")


@pytest.mark.django_db
def test_rejects_when_kyc_not_approved():
    from apps.withdrawal.services import KycNotApproved, submit_withdrawal_request

    distributor = _make_eligible_distributor()
    distributor.kyc_status = Distributor.KycStatus.PENDING
    distributor.save()

    with pytest.raises(KycNotApproved):
        submit_withdrawal_request(distributor, Decimal("500.00"))


@pytest.mark.django_db
def test_rejects_when_payout_destination_not_set():
    from apps.withdrawal.services import (
        PayoutDestinationNotSet,
        submit_withdrawal_request,
    )

    distributor = _make_eligible_distributor()
    distributor.mobile_money_number = ""
    distributor.mobile_money_network = ""
    distributor.save()

    with pytest.raises(PayoutDestinationNotSet):
        submit_withdrawal_request(distributor, Decimal("500.00"))


@pytest.mark.django_db
def test_rejects_amount_below_minimum():
    from apps.withdrawal.services import BelowMinimumAmount, submit_withdrawal_request

    distributor = _make_eligible_distributor()

    with pytest.raises(BelowMinimumAmount):
        submit_withdrawal_request(distributor, Decimal("50.00"))


@pytest.mark.django_db
def test_rejects_amount_above_maximum():
    from apps.withdrawal.services import AboveMaximumAmount, submit_withdrawal_request

    distributor = _make_eligible_distributor(balance=Decimal("20000.00"))

    with pytest.raises(AboveMaximumAmount):
        submit_withdrawal_request(distributor, Decimal("15000.00"))


@pytest.mark.django_db
def test_rejects_amount_exceeding_wallet_balance_even_within_min_max():
    from apps.withdrawal.services import (
        InsufficientWalletBalance,
        submit_withdrawal_request,
    )

    distributor = _make_eligible_distributor(balance=Decimal("200.00"))

    with pytest.raises(InsufficientWalletBalance):
        submit_withdrawal_request(distributor, Decimal("500.00"))


@pytest.mark.django_db
def test_second_request_within_the_same_window_is_rejected():
    from apps.withdrawal.services import (
        WithdrawalWindowActive,
        submit_withdrawal_request,
    )

    distributor = _make_eligible_distributor(balance=Decimal("2000.00"))
    submit_withdrawal_request(distributor, Decimal("500.00"))

    with pytest.raises(WithdrawalWindowActive):
        submit_withdrawal_request(distributor, Decimal("200.00"))


@pytest.mark.django_db
def test_window_boundary_one_second_before_expiry_still_blocked():
    from apps.withdrawal.models import WithdrawalRequest
    from apps.withdrawal.services import (
        WithdrawalWindowActive,
        submit_withdrawal_request,
    )

    distributor = _make_eligible_distributor(balance=Decimal("2000.00"))
    first = submit_withdrawal_request(distributor, Decimal("500.00"))
    # WITHDRAWAL_FREQUENCY defaults to "weekly" (7 days). Backdate the
    # first request to exactly 7 days minus 1 second ago -- the window has
    # not yet fully elapsed, so a second attempt must still be blocked.
    backdated = datetime.now(dt_timezone.utc) - timedelta(days=7, seconds=-1)
    WithdrawalRequest.objects.filter(pk=first.pk).update(created_at=backdated)

    with pytest.raises(WithdrawalWindowActive):
        submit_withdrawal_request(distributor, Decimal("200.00"))


@pytest.mark.django_db
def test_window_boundary_exactly_at_expiry_is_allowed():
    from apps.withdrawal.models import WithdrawalRequest
    from apps.withdrawal.services import submit_withdrawal_request

    distributor = _make_eligible_distributor(balance=Decimal("2000.00"))
    first = submit_withdrawal_request(distributor, Decimal("500.00"))
    # Exactly 7 days ago -- the window has fully elapsed, must be allowed.
    backdated = datetime.now(dt_timezone.utc) - timedelta(days=7)
    WithdrawalRequest.objects.filter(pk=first.pk).update(created_at=backdated)

    second = submit_withdrawal_request(distributor, Decimal("200.00"))

    assert second.pk is not None
    assert second.pk != first.pk


@pytest.mark.django_db
def test_a_rejected_prior_request_does_not_block_resubmission_in_the_same_window():
    from apps.withdrawal.models import WithdrawalRequest
    from apps.withdrawal.services import submit_withdrawal_request

    distributor = _make_eligible_distributor(balance=Decimal("2000.00"))
    first = submit_withdrawal_request(distributor, Decimal("500.00"))
    first.status = WithdrawalRequest.Status.REJECTED
    first.save()

    second = submit_withdrawal_request(distributor, Decimal("200.00"))

    assert second.pk is not None


@pytest.mark.django_db
def test_a_payout_failed_reversed_prior_request_does_not_block_resubmission():
    from apps.withdrawal.models import WithdrawalRequest
    from apps.withdrawal.services import submit_withdrawal_request

    distributor = _make_eligible_distributor(balance=Decimal("2000.00"))
    first = submit_withdrawal_request(distributor, Decimal("500.00"))
    first.status = WithdrawalRequest.Status.PAYOUT_FAILED_REVERSED
    first.save()

    second = submit_withdrawal_request(distributor, Decimal("200.00"))

    assert second.pk is not None


@pytest.mark.django_db(transaction=True)
def test_concurrent_submissions_never_both_succeed_within_the_window():
    """Mirrors tests/unit/wallet/test_wallet_service.py's
    test_concurrent_debits_never_overdraw_the_balance proof, for the new
    failure mode a naive check-then-act window check introduces: five
    simultaneous submission attempts for the same distributor must let
    exactly one succeed and the rest fail with WithdrawalWindowActive --
    never let two both slip past the once-per-window check."""
    from apps.withdrawal.models import WithdrawalRequest
    from apps.withdrawal.services import (
        WithdrawalWindowActive,
        submit_withdrawal_request,
    )

    distributor = _make_eligible_distributor(balance=Decimal("2000.00"))
    successes = []
    window_errors = []
    lock = threading.Lock()

    def attempt(i):
        try:
            request = retry_on_lock_contention(
                lambda: submit_withdrawal_request(distributor, Decimal("100.00"))
            )
            with lock:
                successes.append(request)
        except WithdrawalWindowActive as exc:
            with lock:
                window_errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(successes) == 1
    assert len(window_errors) == 4
    assert WithdrawalRequest.objects.filter(distributor=distributor).count() == 1


@pytest.mark.django_db
def test_rejects_cleanly_when_tax_rate_is_above_100_percent():
    """code-review-and-quality (2026-07-23): a misconfigured rate above
    100% must raise a clean, catchable exception -- not let a negative
    net_amount reach WithdrawalRequest.objects.create() and surface as an
    opaque IntegrityError from the model's own CheckConstraint."""
    from constance import config

    from apps.withdrawal.services import (
        WithholdingTaxMisconfigured,
        submit_withdrawal_request,
    )

    distributor = _make_eligible_distributor()
    original_rate = config.WITHHOLDING_TAX_RATE
    config.WITHHOLDING_TAX_RATE = Decimal("150")
    try:
        with pytest.raises(WithholdingTaxMisconfigured):
            submit_withdrawal_request(distributor, Decimal("500.00"))
    finally:
        config.WITHHOLDING_TAX_RATE = original_rate


@pytest.mark.django_db
def test_rejects_cleanly_when_tax_rate_is_negative():
    """The original defensive check only covered net_amount < 0 (rate
    above 100%) -- a negative rate produces tax_amount < 0 with
    net_amount > amount instead, sailing past that single check entirely
    and hitting the CheckConstraint from the other direction. Both signs
    must be caught."""
    from constance import config

    from apps.withdrawal.services import (
        WithholdingTaxMisconfigured,
        submit_withdrawal_request,
    )

    distributor = _make_eligible_distributor()
    original_rate = config.WITHHOLDING_TAX_RATE
    config.WITHHOLDING_TAX_RATE = Decimal("-10")
    try:
        with pytest.raises(WithholdingTaxMisconfigured):
            submit_withdrawal_request(distributor, Decimal("500.00"))
    finally:
        config.WITHHOLDING_TAX_RATE = original_rate


@pytest.mark.django_db
def test_rejects_cleanly_when_withdrawal_frequency_is_unknown():
    from constance import config

    from apps.withdrawal.services import (
        WithdrawalFrequencyMisconfigured,
        submit_withdrawal_request,
    )

    distributor = _make_eligible_distributor()
    original_frequency = config.WITHDRAWAL_FREQUENCY
    config.WITHDRAWAL_FREQUENCY = "monthly"
    try:
        with pytest.raises(WithdrawalFrequencyMisconfigured):
            submit_withdrawal_request(distributor, Decimal("500.00"))
    finally:
        config.WITHDRAWAL_FREQUENCY = original_frequency
