from datetime import timedelta
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.utils import timezone

import pytest
from constance import config

from apps.commissions.services import (
    apply_weekly_binary_bonus_cap,
    calculate_binary_bonus,
)
from apps.distributors.models import Distributor
from apps.wallet.models import Wallet, WalletTransaction

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233244{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _make_transaction(distributor, amount, transaction_type, days_ago=0):
    wallet, _ = Wallet.objects.get_or_create(distributor=distributor)
    transaction = WalletTransaction.objects.create(
        wallet=wallet,
        amount=amount,
        transaction_type=transaction_type,
        reference="test-ref",
    )
    if days_ago:
        WalletTransaction.objects.filter(pk=transaction.pk).update(
            created_at=timezone.now() - timedelta(days=days_ago)
        )
    return transaction


@pytest.mark.django_db
def test_reproduces_the_doc_example_600_pv_weak_leg():
    assert calculate_binary_bonus(weak_leg_pv=600) == Decimal("45.00")


@pytest.mark.django_db
def test_zero_weak_leg_produces_zero_bonus():
    assert calculate_binary_bonus(weak_leg_pv=0) == Decimal("0.00")


@pytest.mark.django_db
def test_rounds_to_the_pesewa_on_a_non_round_value():
    """500 PV x 10.333% = 51.665 exactly -- a genuine half-pesewa case,
    proving rounding is ROUND_HALF_UP. Mirrors
    test_direct_referral.py::test_rounds_to_the_pesewa_on_a_non_round_value."""
    original_rate = config.BINARY_BONUS_RATE
    config.BINARY_BONUS_RATE = Decimal("10.333")
    try:
        assert calculate_binary_bonus(weak_leg_pv=500) == Decimal("51.67")
    finally:
        config.BINARY_BONUS_RATE = original_rate


@pytest.mark.django_db
def test_full_raw_bonus_passes_through_with_no_prior_payments_this_week():
    distributor = _make_distributor()

    result = apply_weekly_binary_bonus_cap(distributor, Decimal("45.00"))

    assert result == Decimal("45.00")


@pytest.mark.django_db
def test_payout_is_reduced_to_exactly_fill_remaining_room_under_the_cap():
    distributor = _make_distributor()
    already_paid = config.WEEKLY_BINARY_BONUS_CAP - Decimal("10.00")
    _make_transaction(
        distributor, already_paid, WalletTransaction.TransactionType.BINARY_BONUS
    )

    result = apply_weekly_binary_bonus_cap(distributor, Decimal("45.00"))

    assert result == Decimal("10.00")


@pytest.mark.django_db
def test_already_at_the_cap_returns_zero():
    distributor = _make_distributor()
    _make_transaction(
        distributor,
        config.WEEKLY_BINARY_BONUS_CAP,
        WalletTransaction.TransactionType.BINARY_BONUS,
    )

    result = apply_weekly_binary_bonus_cap(distributor, Decimal("45.00"))

    assert result == Decimal("0.00")


@pytest.mark.django_db
def test_already_over_the_cap_returns_zero_not_negative():
    distributor = _make_distributor()
    _make_transaction(
        distributor,
        config.WEEKLY_BINARY_BONUS_CAP + Decimal("500.00"),
        WalletTransaction.TransactionType.BINARY_BONUS,
    )

    result = apply_weekly_binary_bonus_cap(distributor, Decimal("45.00"))

    assert result == Decimal("0.00")


@pytest.mark.django_db
def test_a_payment_from_eight_days_ago_does_not_count_against_the_cap():
    distributor = _make_distributor()
    _make_transaction(
        distributor,
        config.WEEKLY_BINARY_BONUS_CAP,
        WalletTransaction.TransactionType.BINARY_BONUS,
        days_ago=8,
    )

    result = apply_weekly_binary_bonus_cap(distributor, Decimal("45.00"))

    assert result == Decimal("45.00")


@pytest.mark.django_db
def test_a_direct_referral_bonus_payment_does_not_count_against_this_cap():
    distributor = _make_distributor()
    _make_transaction(
        distributor,
        config.WEEKLY_BINARY_BONUS_CAP,
        WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
    )

    result = apply_weekly_binary_bonus_cap(distributor, Decimal("45.00"))

    assert result == Decimal("45.00")
