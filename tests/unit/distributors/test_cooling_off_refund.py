from datetime import timedelta
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone

import pytest
from constance import config

from apps.binary_tree.models import BinaryTreeEdge
from apps.distributors.cooling_off_services import (
    CoolingOffPeriodExpired,
    NoRefundableStarterPackPurchase,
    cancel_membership_and_refund,
)
from apps.distributors.models import Distributor
from apps.distributors.services import (
    MembershipCancelled,
    consume_paid_starter_pack,
    snapshot_starter_pack_choice,
)
from apps.pv_ledger.models import MonthlyPersonalPv, PvLedger
from apps.wallet.models import Wallet, WalletTransaction
from apps.wallet.services import credit as credit_wallet
from apps.wallet.services import debit as debit_wallet

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(sponsor=None):
    phone = f"+233242{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone, sponsor=sponsor)


def _select_pack_b(distributor, reference):
    distributor.starter_pack_choice = "B"
    distributor.starter_pack_price_pesewas = int(config.STARTER_PACK_B_PRICE * 100)
    distributor.starter_pack_pv = config.STARTER_PACK_B_PV
    distributor.starter_pack_rank = config.STARTER_PACK_B_RANK
    distributor.starter_pack_payment_reference = reference
    distributor.save(
        update_fields=[
            "starter_pack_choice",
            "starter_pack_price_pesewas",
            "starter_pack_pv",
            "starter_pack_rank",
            "starter_pack_payment_reference",
        ]
    )
    return distributor


def _confirm_pack_b_purchase(distributor, reference):
    """Runs the real consume_paid_starter_pack path (Paystack mocked) so
    PV/rank/the sponsor's direct referral bonus are all genuinely
    credited exactly the way production does it -- the reversal being
    tested here must undo real, realistically-created state, not a
    hand-assembled fixture."""
    with patch("apps.distributors.services.verify_transaction") as mock_verify:
        mock_verify.return_value = {
            "status": "success",
            "amount": distributor.starter_pack_price_pesewas,
            "currency": "GHS",
        }
        consume_paid_starter_pack(reference)
    distributor.refresh_from_db()
    return distributor


def _confirmed_pack_b_distributor(sponsor=None, reference="pack-ref-1"):
    distributor = _select_pack_b(_make_distributor(sponsor=sponsor), reference)
    return _confirm_pack_b_purchase(distributor, reference)


# --- Refund amount + eligibility window --------------------------------------


@pytest.mark.django_db
def test_refund_amount_matches_the_docs_worked_example():
    """Section 9's own example: Pack B GHS 2,000, 10% processing fee ->
    GHS 1,800 refunded."""
    distributor = _confirmed_pack_b_distributor()

    refund = cancel_membership_and_refund(distributor.pk)

    assert refund == Decimal("1800.00")
    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("1800.00")
    txn = WalletTransaction.objects.get(
        wallet=wallet,
        transaction_type=WalletTransaction.TransactionType.COOLING_OFF_REFUND,
    )
    assert txn.amount == Decimal("1800.00")


@pytest.mark.django_db
def test_a_request_after_the_cooling_off_window_is_rejected():
    distributor = _confirmed_pack_b_distributor()
    distributor.starter_pack_confirmed_at = timezone.now() - timedelta(days=8)
    distributor.save(update_fields=["starter_pack_confirmed_at"])

    with pytest.raises(CoolingOffPeriodExpired):
        cancel_membership_and_refund(distributor.pk)

    assert not Wallet.objects.filter(distributor=distributor).exists()


@pytest.mark.django_db
def test_a_request_on_day_seven_exactly_still_succeeds():
    distributor = _confirmed_pack_b_distributor()
    distributor.starter_pack_confirmed_at = timezone.now() - timedelta(
        days=config.COOLING_OFF_PERIOD_DAYS, minutes=-5
    )
    distributor.save(update_fields=["starter_pack_confirmed_at"])

    refund = cancel_membership_and_refund(distributor.pk)  # must not raise

    assert refund == Decimal("1800.00")


@pytest.mark.django_db
def test_no_confirmed_starter_pack_purchase_raises():
    distributor = _make_distributor()  # registered, never bought a pack

    with pytest.raises(NoRefundableStarterPackPurchase):
        cancel_membership_and_refund(distributor.pk)


# --- PV reversal --------------------------------------------------------------


@pytest.mark.django_db
def test_pv_is_reversed_for_both_ancestor_legs_and_personal_pv():
    sponsor = _make_distributor()
    PvLedger.objects.create(distributor=sponsor, left_leg_pv=0, right_leg_pv=0)
    distributor = _confirmed_pack_b_distributor(sponsor=sponsor)

    cancel_membership_and_refund(distributor.pk)

    ledger = PvLedger.objects.get(distributor=sponsor)
    assert ledger.left_leg_pv + ledger.right_leg_pv == 0
    personal = MonthlyPersonalPv.objects.get(distributor=distributor)
    assert personal.pv == 0


# --- Sponsor's direct referral bonus reversal ---------------------------------


@pytest.mark.django_db
def test_sponsors_direct_referral_bonus_is_reversed_when_balance_is_sufficient():
    sponsor = _make_distributor()
    distributor = _confirmed_pack_b_distributor(sponsor=sponsor)
    sponsor_wallet = Wallet.objects.get(distributor=sponsor)
    assert sponsor_wallet.balance == Decimal("100.00")  # sanity: bonus was credited

    cancel_membership_and_refund(distributor.pk)

    sponsor_wallet.refresh_from_db()
    assert sponsor_wallet.balance == Decimal("0.00")
    reversal = WalletTransaction.objects.get(
        wallet=sponsor_wallet,
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS_REVERSAL,
    )
    assert reversal.amount == Decimal("-100.00")


@pytest.mark.django_db
def test_reversal_amount_is_unaffected_by_a_later_bonus_rate_change():
    """Regression test (doubt-driven-development finding, pre-
    implementation review): the reversal must use the amount actually
    credited at signup, not recompute via the live
    DIRECT_REFERRAL_BONUS_RATE -- otherwise an admin rate change between
    signup and cancellation would claw back the wrong amount, either
    leaving the sponsor with an unearned surplus or attempting to debit
    more than they ever received."""
    sponsor = _make_distributor()
    distributor = _confirmed_pack_b_distributor(sponsor=sponsor)
    # Surplus balance so BOTH the correct amount (GHS 100, what was
    # actually credited) and a hypothetical live-rate-recomputed amount
    # are fully payable -- otherwise an available-balance cap could mask
    # the bug this test exists to catch (a debit silently capped to what's
    # available looks identical whether the requested amount was right or
    # wrong).
    credit_wallet(
        sponsor,
        Decimal("200.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="unrelated-binary-bonus",
    )
    sponsor_wallet = Wallet.objects.get(distributor=sponsor)
    assert sponsor_wallet.balance == Decimal("300.00")

    original_rate = config.DIRECT_REFERRAL_BONUS_RATE
    config.DIRECT_REFERRAL_BONUS_RATE = Decimal("2")  # was 10% at signup
    try:
        cancel_membership_and_refund(distributor.pk)
    finally:
        config.DIRECT_REFERRAL_BONUS_RATE = original_rate

    sponsor_wallet.refresh_from_db()
    # Only correct if GHS 100 (the amount actually credited at signup, 10%
    # x 1000 PV) was reversed, not GHS 20 (2% x 1000 PV, a live-rate
    # recompute).
    assert sponsor_wallet.balance == Decimal("200.00")


@pytest.mark.django_db
def test_insufficient_sponsor_balance_logs_a_shortfall_and_refund_still_completes(
    caplog,
):
    sponsor = _make_distributor()
    distributor = _confirmed_pack_b_distributor(sponsor=sponsor)
    debit_wallet(
        sponsor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="withdrawal-1",
    )
    sponsor_wallet = Wallet.objects.get(distributor=sponsor)
    assert sponsor_wallet.balance == Decimal("0.00")  # sponsor already withdrew it

    with caplog.at_level("WARNING"):
        refund = cancel_membership_and_refund(distributor.pk)

    assert refund == Decimal("1800.00")
    assert "short by" in caplog.text.lower()
    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("1800.00")
    sponsor_wallet.refresh_from_db()
    assert sponsor_wallet.balance == Decimal("0.00")  # never driven negative
    assert not WalletTransaction.objects.filter(
        wallet=sponsor_wallet,
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS_REVERSAL,
    ).exists()


@pytest.mark.django_db
def test_a_partial_sponsor_balance_debits_only_whats_available():
    sponsor = _make_distributor()
    distributor = _confirmed_pack_b_distributor(sponsor=sponsor)
    debit_wallet(
        sponsor,
        Decimal("70.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="withdrawal-1",
    )
    sponsor_wallet = Wallet.objects.get(distributor=sponsor)
    assert sponsor_wallet.balance == Decimal("30.00")

    cancel_membership_and_refund(distributor.pk)

    sponsor_wallet.refresh_from_db()
    assert sponsor_wallet.balance == Decimal("0.00")
    reversal = WalletTransaction.objects.get(
        wallet=sponsor_wallet,
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS_REVERSAL,
    )
    assert reversal.amount == Decimal("-30.00")


@pytest.mark.django_db
def test_root_distributor_with_no_sponsor_has_nothing_to_reverse():
    distributor = _confirmed_pack_b_distributor(sponsor=None)

    refund = cancel_membership_and_refund(distributor.pk)  # must not raise

    assert refund == Decimal("1800.00")
    assert not Wallet.objects.exclude(distributor=distributor).exists()


# --- Account state + idempotency ----------------------------------------------


@pytest.mark.django_db
def test_account_is_deactivated_but_binary_tree_placement_is_untouched():
    sponsor = _make_distributor()
    distributor = _confirmed_pack_b_distributor(sponsor=sponsor)
    assert BinaryTreeEdge.objects.filter(descendant=distributor).exists()

    cancel_membership_and_refund(distributor.pk)

    assert User.objects.get(pk=distributor.user_id).is_active is False
    assert BinaryTreeEdge.objects.filter(descendant=distributor).exists()


@pytest.mark.django_db
def test_a_second_cancellation_is_a_safe_idempotent_no_op():
    distributor = _confirmed_pack_b_distributor()

    first = cancel_membership_and_refund(distributor.pk)
    second = cancel_membership_and_refund(distributor.pk)

    assert first == Decimal("1800.00")
    assert second is None
    wallet = Wallet.objects.get(distributor=distributor)
    assert wallet.balance == Decimal("1800.00")
    assert (
        WalletTransaction.objects.filter(
            wallet=wallet,
            transaction_type=WalletTransaction.TransactionType.COOLING_OFF_REFUND,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_cancelled_distributor_cannot_reselect_a_starter_pack_even_if_reactivated():
    """Regression test (doubt-driven-development finding, pre-
    implementation review): apps/admin_portal's suspend/reactivate toggle
    flips user.is_active with no knowledge of cooling-off cancellation.
    Without this guard, a reactivated distributor could re-select and
    re-pay for a starter pack -- and since BinaryTreeEdge placement is
    deliberately never removed (ADR-0007), consume_paid_starter_pack's
    AlreadyPlacedError-catch path would credit PV and the sponsor's bonus
    a second time for a position that was already refunded once."""
    distributor = _confirmed_pack_b_distributor()

    cancel_membership_and_refund(distributor.pk)

    distributor.user.is_active = True  # simulates an admin reactivation
    distributor.user.save(update_fields=["is_active"])

    with pytest.raises(MembershipCancelled):
        snapshot_starter_pack_choice(distributor.pk, "A")


@pytest.mark.django_db
def test_replaying_the_old_paystack_reference_after_cancellation_is_a_no_op():
    """Regression test (security-and-hardening review, post-
    implementation): cancel_membership_and_refund clears
    starter_pack_confirmed_at back to None, so consume_paid_starter_pack's
    own idempotency check (`starter_pack_confirmed_at is not None`) no
    longer protects against a REPLAYED webhook/callback for the same old
    reference -- a genuinely successful Paystack transaction stays
    verifiable as "success" forever. starter_pack_price_pesewas is also
    cleared, which would incidentally catch this via the amount-mismatch
    check -- so this test manually restores it, to prove the explicit
    cooling_off_cancelled_at guard is what's actually stopping this, not
    that incidental side effect."""
    sponsor = _make_distributor()
    distributor = _confirmed_pack_b_distributor(sponsor=sponsor)
    reference = distributor.starter_pack_payment_reference
    cancel_membership_and_refund(distributor.pk)

    # Simulate the incidental protection not being there.
    distributor.refresh_from_db()
    distributor.starter_pack_price_pesewas = int(config.STARTER_PACK_B_PRICE * 100)
    distributor.starter_pack_payment_reference = reference
    distributor.save(
        update_fields=["starter_pack_price_pesewas", "starter_pack_payment_reference"]
    )
    edge_count_before = BinaryTreeEdge.objects.filter(descendant=distributor).count()

    with patch("apps.distributors.services.verify_transaction") as mock_verify:
        mock_verify.return_value = {
            "status": "success",
            "amount": int(config.STARTER_PACK_B_PRICE * 100),
            "currency": "GHS",
        }
        consume_paid_starter_pack(reference)  # must not raise or re-credit

    distributor.refresh_from_db()
    assert distributor.rank == ""
    assert distributor.starter_pack_confirmed_at is None
    assert (
        BinaryTreeEdge.objects.filter(descendant=distributor).count()
        == edge_count_before
    )
    sponsor_wallet = Wallet.objects.get(distributor=sponsor)
    # The original bonus was already reversed by cancellation (balance 0)
    # -- a re-credit here would show up as a nonzero balance again.
    assert sponsor_wallet.balance == Decimal("0.00")
