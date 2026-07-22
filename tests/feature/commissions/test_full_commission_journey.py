"""Closes the Checkpoint E gap noted in tasks/plan.md: each bonus type
(Task 12 Direct Referral, Task 13 Binary, Task 14 Matching) already
reproduces its own Section 14 worked example in isolation, but no single
test chains registration -> referral bonus -> binary bonus -> matching
bonus end to end through the real service/task functions. This file is
that missing chain.

Uses this codebase's own already-resolved Direct Referral Bonus numbers
(rate x PV: Pack A = GHS 50, Pack B = GHS 100), not Section 14's literal
GHS 200 / GHS 75 narrative figures -- those are documented, confirmed
narrative errors in the original requirements doc (see CLAUDE.md's
"Formula confirmed by the user, not read directly from spec" note and
tests/unit/commissions/test_direct_referral.py), not a second valid
interpretation to reproduce.

Deliberately at the service-function layer (consume_paid_starter_pack,
calculate_binary_bonus, calculate_matching_bonus), not the HTTP/webhook
layer -- the registration/payment views and Paystack webhook signature
verification are already covered by tests/feature/distributors/
test_registration_payment.py and test_starter_pack.py. This test's job is
to prove the COMMISSION PIPELINE composes correctly once a purchase is
confirmed, which none of those existing tests exercise past the direct
referral bonus.

No timezone.now() mocking -- every step (purchases, both Celery task
calls) runs in real wall-clock time within one test execution, so the
7-day rolling windows, PvDailyBucket dates, and MonthlyPersonalPv periods
all naturally align without needing to patch time at multiple call sites.
"""

from decimal import ROUND_HALF_UP, Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone

import pytest
from constance import config

from apps.commissions.tasks import calculate_binary_bonus, calculate_matching_bonus
from apps.distributors.models import Distributor
from apps.distributors.services import consume_paid_starter_pack
from apps.pv_ledger.models import MonthlyPersonalPv, PvLedger
from apps.wallet.models import Wallet, WalletTransaction

User = get_user_model()
_phone_seq = count(1)


def _make_distributor(sponsor=None):
    phone = f"+233248{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone, sponsor=sponsor)


def _buy_starter_pack(distributor, choice, reference):
    """Mirrors tests/unit/distributors/test_consume_paid_starter_pack.py's
    established pattern -- pins the same fields snapshot_starter_pack_
    choice would, then confirms via the real consume_paid_starter_pack
    with Paystack verification mocked (not the HTTP/webhook layer, which
    is covered elsewhere)."""
    if choice == "A":
        price, pv, rank = (
            config.STARTER_PACK_A_PRICE,
            config.STARTER_PACK_A_PV,
            config.STARTER_PACK_A_RANK,
        )
    else:
        price, pv, rank = (
            config.STARTER_PACK_B_PRICE,
            config.STARTER_PACK_B_PV,
            config.STARTER_PACK_B_RANK,
        )
    distributor.starter_pack_choice = choice
    distributor.starter_pack_price_pesewas = int(price * 100)
    distributor.starter_pack_pv = pv
    distributor.starter_pack_rank = rank
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
    return int(price * 100)


def _wallet_balance(distributor):
    wallet, _ = Wallet.objects.get_or_create(distributor=distributor)
    return wallet.balance


@pytest.mark.django_db
def test_registration_through_binary_and_matching_bonus_composes_correctly():
    # -- Efua: the pre-existing distributor this journey starts from. A
    # real onboarding wasn't simulated for her (she has no purchase of her
    # own in this story) -- her rank and this month's personal PV are set
    # directly, standing in for "already met her personal PV requirement
    # through prior activity," which is the only thing about her this test
    # takes as given rather than derives.
    efua = _make_distributor(sponsor=None)
    Distributor.objects.filter(pk=efua.pk).update(rank="bronze")
    efua.refresh_from_db()
    MonthlyPersonalPv.objects.create(
        distributor=efua,
        period=timezone.now().date().replace(day=1),
        pv=config.MIN_MONTHLY_PERSONAL_PV,
    )

    with patch("apps.distributors.services.verify_transaction") as mock_verify:
        # -- GrandRoot: recruited by Efua, buys Pack A (500 PV).
        grandroot = _make_distributor(sponsor=efua)
        amount_pesewas = _buy_starter_pack(grandroot, "A", "journey-grandroot")
        mock_verify.return_value = {
            "status": "success",
            "amount": amount_pesewas,
            "currency": "GHS",
        }
        consume_paid_starter_pack("journey-grandroot")

        # -- Step 1 verified: Direct Referral Bonus. Efua earns 10% of
        # GrandRoot's 500 PV = GHS 50 (this project's resolved formula,
        # not Section 14's disputed literal figure).
        grandroot.refresh_from_db()
        assert grandroot.rank == "bronze"
        assert _wallet_balance(efua) == Decimal("50.00")
        assert WalletTransaction.objects.filter(
            wallet__distributor=efua,
            transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        ).exists()

        # -- Kofi and Ama: both recruited by GrandRoot, buying different
        # packs so their PV lands on different amounts regardless of which
        # physical leg (left/right) auto-balance sends each to.
        kofi = _make_distributor(sponsor=grandroot)
        amount_pesewas = _buy_starter_pack(kofi, "A", "journey-kofi")  # 500 PV
        mock_verify.return_value = {
            "status": "success",
            "amount": amount_pesewas,
            "currency": "GHS",
        }
        consume_paid_starter_pack("journey-kofi")

        ama = _make_distributor(sponsor=grandroot)
        amount_pesewas = _buy_starter_pack(ama, "B", "journey-ama")  # 1000 PV
        mock_verify.return_value = {
            "status": "success",
            "amount": amount_pesewas,
            "currency": "GHS",
        }
        consume_paid_starter_pack("journey-ama")

    # -- Step 2 verified: GrandRoot's own Direct Referral Bonus, from her
    # two personal recruits -- GHS 50 (Kofi, Pack A) + GHS 100 (Ama, Pack
    # B) = GHS 150, on top of the referral bonus chain above.
    assert _wallet_balance(grandroot) == Decimal("150.00")

    # -- Step 3 verified: binary tree placement + write-time PV crediting
    # actually happened -- GrandRoot's two legs hold Kofi's and Ama's PV
    # (500 and 1000, in either order), not stacked on one leg.
    grandroot_ledger = PvLedger.objects.get(distributor=grandroot)
    leg_amounts = sorted([grandroot_ledger.left_leg_pv, grandroot_ledger.right_leg_pv])
    assert leg_amounts == [500, 1000]

    # -- Step 4: run the real Binary Bonus Celery Beat task (not the lower-
    # level per-distributor function) -- exercises distributor_ids_with_
    # pending_pv()'s selection query, not just the payout math.
    calculate_binary_bonus()

    # -- Binary Bonus verified: GrandRoot's weak leg (500, whichever
    # physical leg that ended up being) x BINARY_BONUS_RATE (7.5% default)
    # = GHS 37.50.
    expected_binary_bonus = (
        Decimal(500) * config.BINARY_BONUS_RATE / Decimal(100)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert expected_binary_bonus == Decimal("37.50")
    grandroot_binary_bonus_txn = WalletTransaction.objects.get(
        wallet__distributor=grandroot,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
    )
    assert grandroot_binary_bonus_txn.amount == Decimal("37.50")

    # -- Step 5: run the real Matching Bonus Celery Beat task. Efua's
    # downline (GrandRoot, level 1 via the sponsor chain) earned that
    # GHS 37.50 BINARY_BONUS -- Efua's own DIRECT_REFERRAL_BONUS earnings
    # and GrandRoot's GHS 150 of DIRECT_REFERRAL_BONUS must NOT be swept
    # in (sum_downline_binary_bonus_earnings filters by transaction_type),
    # proving that filter holds under a real, mixed-transaction-type
    # wallet history, not just a synthetic one.
    calculate_matching_bonus()

    # -- Matching Bonus verified: 5% (default MATCHING_BONUS_RATE) of
    # GrandRoot's GHS 37.50 binary bonus = GHS 1.875, ROUND_HALF_UP to
    # GHS 1.88.
    expected_matching_bonus = (
        Decimal("37.50") * config.MATCHING_BONUS_RATE / Decimal(100)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    assert expected_matching_bonus == Decimal("1.88")
    efua_matching_bonus_txn = WalletTransaction.objects.get(
        wallet__distributor=efua,
        transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS,
    )
    assert efua_matching_bonus_txn.amount == Decimal("1.88")

    # -- Efua's full earnings across the whole journey: GHS 50 (her own
    # direct referral bonus from GrandRoot's purchase) + GHS 1.88
    # (matching bonus off GrandRoot's binary bonus) -- proves the two
    # bonus types accumulate independently in the same wallet, not one
    # overwriting the other.
    assert _wallet_balance(efua) == Decimal("51.88")

    # -- GrandRoot earns no matching bonus of her own this cycle: her
    # downline (Kofi, Ama) never earned a binary bonus themselves (neither
    # has any downline PV of their own) -- proves matching bonus only
    # pays on ACTUAL downline binary-bonus earnings, not merely on having
    # a qualifying rank and some downline.
    assert not WalletTransaction.objects.filter(
        wallet__distributor=grandroot,
        transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS,
    ).exists()
