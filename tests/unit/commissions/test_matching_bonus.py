from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model

import pytest
from constance import config

from apps.commissions.services import (
    calculate_matching_bonus,
    process_matching_bonus_for_distributor,
    sum_downline_binary_bonus_earnings,
)
from apps.distributors.models import Distributor
from apps.pv_ledger.models import MonthlyPersonalPv
from apps.wallet.models import Wallet, WalletTransaction

User = get_user_model()
_phone_seq = count(1)
_reference_seq = count(1)

RUN_AT = datetime(2020, 3, 10, 10, 0, tzinfo=dt_timezone.utc)


def _make_distributor(sponsor=None):
    phone = f"+233245{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone, sponsor=sponsor)


def _make_binary_bonus_transaction(distributor, amount, run_at=RUN_AT, days_ago=1):
    wallet, _ = Wallet.objects.get_or_create(distributor=distributor)
    transaction = WalletTransaction.objects.create(
        wallet=wallet,
        amount=amount,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference=f"test-ref-{distributor.pk}-{next(_reference_seq)}",
    )
    WalletTransaction.objects.filter(pk=transaction.pk).update(
        created_at=run_at - timedelta(days=days_ago)
    )
    return transaction


# ---------------------------------------------------------------------------
# calculate_matching_bonus -- pure rate x total, mirrors calculate_binary_bonus
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reproduces_the_doc_example_450_total():
    """Level 1 GHS200 + Level 2 GHS150 + Level 3 GHS100 = GHS450 total ->
    5% = GHS22.50, matching the doc example exactly."""
    assert calculate_matching_bonus(Decimal("450")) == Decimal("22.50")


@pytest.mark.django_db
def test_zero_total_produces_zero_bonus():
    assert calculate_matching_bonus(Decimal("0")) == Decimal("0.00")


@pytest.mark.django_db
def test_rounds_to_the_pesewa_on_a_non_round_value():
    original_rate = config.MATCHING_BONUS_RATE
    config.MATCHING_BONUS_RATE = Decimal("10.333")
    try:
        # 500 * 10.333% = 51.665 exactly -- a genuine half-pesewa case.
        assert calculate_matching_bonus(Decimal("500")) == Decimal("51.67")
    finally:
        config.MATCHING_BONUS_RATE = original_rate


# ---------------------------------------------------------------------------
# sum_downline_binary_bonus_earnings -- sponsor-chain BFS + rolling-7-day sum
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reproduces_the_doc_example_level_1_2_3():
    root = _make_distributor()
    level1 = _make_distributor(sponsor=root)
    level2 = _make_distributor(sponsor=level1)
    level3 = _make_distributor(sponsor=level2)
    _make_binary_bonus_transaction(level1, Decimal("200"))
    _make_binary_bonus_transaction(level2, Decimal("150"))
    _make_binary_bonus_transaction(level3, Decimal("100"))

    total = sum_downline_binary_bonus_earnings(root, max_depth=3, run_at=RUN_AT)

    assert total == Decimal("450")


@pytest.mark.django_db
def test_unlimited_depth_reaches_a_level_4_plus_recruit():
    """Silver-rank distributors pass max_depth=None -- must keep walking
    past level 3, unlike Bronze's capped-at-3 behavior."""
    root = _make_distributor()
    chain = root
    for _ in range(5):
        chain = _make_distributor(sponsor=chain)
    _make_binary_bonus_transaction(chain, Decimal("60"))  # level 5

    total = sum_downline_binary_bonus_earnings(root, max_depth=None, run_at=RUN_AT)

    assert total == Decimal("60")


@pytest.mark.django_db
def test_bronze_depth_of_3_does_not_reach_a_level_4_recruit():
    root = _make_distributor()
    chain = root
    for _ in range(4):
        chain = _make_distributor(sponsor=chain)
    _make_binary_bonus_transaction(chain, Decimal("60"))  # level 4

    total = sum_downline_binary_bonus_earnings(root, max_depth=3, run_at=RUN_AT)

    assert total == Decimal("0")


@pytest.mark.django_db
def test_max_depth_zero_returns_zero_without_querying_anyone():
    root = _make_distributor()
    level1 = _make_distributor(sponsor=root)
    _make_binary_bonus_transaction(level1, Decimal("200"))

    total = sum_downline_binary_bonus_earnings(root, max_depth=0, run_at=RUN_AT)

    assert total == Decimal("0")


@pytest.mark.django_db
def test_earnings_outside_the_rolling_7_day_window_are_excluded():
    root = _make_distributor()
    level1 = _make_distributor(sponsor=root)
    _make_binary_bonus_transaction(level1, Decimal("200"), days_ago=8)  # too old
    _make_binary_bonus_transaction(level1, Decimal("50"), days_ago=6)  # in window

    total = sum_downline_binary_bonus_earnings(root, max_depth=3, run_at=RUN_AT)

    assert total == Decimal("50")


@pytest.mark.django_db
def test_only_binary_bonus_transactions_count_not_other_transaction_types():
    root = _make_distributor()
    level1 = _make_distributor(sponsor=root)
    wallet, _ = Wallet.objects.get_or_create(distributor=level1)
    WalletTransaction.objects.create(
        wallet=wallet,
        amount=Decimal("999"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="not-binary-bonus",
    )
    _make_binary_bonus_transaction(level1, Decimal("50"))

    total = sum_downline_binary_bonus_earnings(root, max_depth=3, run_at=RUN_AT)

    assert total == Decimal("50")


@pytest.mark.django_db
def test_a_sponsor_chain_cycle_does_not_infinite_loop():
    """The sponsor self-FK has no DB-level cycle protection (unlike
    BinaryTreeEdge, which bans ancestor == descendant). A corrupted graph
    (e.g. A sponsors B, B's sponsor gets reassigned back to A) must not
    hang the whole cycle -- each distributor should only ever be counted
    once, and the walk must terminate."""
    root = _make_distributor()
    a = _make_distributor(sponsor=root)
    b = _make_distributor(sponsor=a)
    Distributor.objects.filter(pk=root.pk).update(
        sponsor=b
    )  # cycle: root -> a -> b -> root
    _make_binary_bonus_transaction(a, Decimal("10"))
    _make_binary_bonus_transaction(b, Decimal("20"))

    total = sum_downline_binary_bonus_earnings(root, max_depth=None, run_at=RUN_AT)

    assert total == Decimal("30")


@pytest.mark.django_db
def test_a_distributor_with_no_downline_returns_zero():
    root = _make_distributor()

    total = sum_downline_binary_bonus_earnings(root, max_depth=3, run_at=RUN_AT)

    assert total == Decimal("0")


@pytest.mark.django_db
def test_the_walk_ceiling_bounds_an_unlimited_depth_walk():
    """2026-07-22 security-and-hardening review: max_depth=None (Silver's
    'unlimited') must still stop at MAX_MATCHING_BONUS_WALK_DEPTH,
    independent of config -- otherwise a pathologically deep sponsor
    chain could make a single call outlast the batch driver's overlap
    lock (which only renews between distributors, never mid-call).
    Patches the ceiling down to 2 rather than constructing 500 real
    distributors, which would make this test slow without proving
    anything the smaller number doesn't already prove -- the mechanism
    being tested (the ceiling check inside the loop) doesn't care what
    the actual number is."""
    import apps.commissions.services as services_module

    root = _make_distributor()
    chain = root
    for _ in range(4):
        chain = _make_distributor(sponsor=chain)
    _make_binary_bonus_transaction(chain, Decimal("60"))  # level 4

    with patch.object(services_module, "MAX_MATCHING_BONUS_WALK_DEPTH", 2):
        total = sum_downline_binary_bonus_earnings(root, max_depth=None, run_at=RUN_AT)

    assert total == Decimal("0")  # level 4 is past the patched ceiling of 2


# ---------------------------------------------------------------------------
# process_matching_bonus_for_distributor -- orchestration, mirrors
# process_binary_bonus_for_distributor's contract (fixed run_at, idempotent,
# row-locked) but read-only against everyone else's data -- no leg/PV
# mutation, since matching bonus never touches another distributor's ledger.
# ---------------------------------------------------------------------------


def _make_eligible(distributor, run_at=RUN_AT):
    MonthlyPersonalPv.objects.create(
        distributor=distributor,
        period=run_at.date().replace(day=1),
        pv=config.MIN_MONTHLY_PERSONAL_PV,
    )


def _wallet_balance(distributor):
    wallet, _ = Wallet.objects.get_or_create(distributor=distributor)
    return wallet.balance


@pytest.mark.django_db
def test_reproduces_the_doc_example_end_to_end():
    root = _make_distributor(sponsor=None)
    Distributor.objects.filter(pk=root.pk).update(rank="bronze")
    root.refresh_from_db()
    _make_eligible(root)
    level1 = _make_distributor(sponsor=root)
    level2 = _make_distributor(sponsor=level1)
    level3 = _make_distributor(sponsor=level2)
    _make_binary_bonus_transaction(level1, Decimal("200"))
    _make_binary_bonus_transaction(level2, Decimal("150"))
    _make_binary_bonus_transaction(level3, Decimal("100"))

    amount = process_matching_bonus_for_distributor(root, RUN_AT)

    assert amount == Decimal("22.50")
    assert _wallet_balance(root) == Decimal("22.50")


@pytest.mark.django_db
def test_ineligible_distributor_gets_no_bonus():
    root = _make_distributor()
    Distributor.objects.filter(pk=root.pk).update(rank="bronze")
    root.refresh_from_db()
    # No MonthlyPersonalPv row -- not eligible.
    level1 = _make_distributor(sponsor=root)
    _make_binary_bonus_transaction(level1, Decimal("200"))

    amount = process_matching_bonus_for_distributor(root, RUN_AT)

    assert amount == Decimal("0.00")
    assert not WalletTransaction.objects.filter(
        transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS
    ).exists()


@pytest.mark.django_db
def test_a_blank_rank_gets_no_bonus_not_a_crash():
    root = _make_distributor()  # rank left at its default "" -- no starter pack yet
    _make_eligible(root)
    level1 = _make_distributor(sponsor=root)
    _make_binary_bonus_transaction(level1, Decimal("200"))

    amount = process_matching_bonus_for_distributor(root, RUN_AT)

    assert amount == Decimal("0.00")


@pytest.mark.django_db
def test_bronze_rank_does_not_reach_a_level_4_recruit_end_to_end():
    root = _make_distributor()
    Distributor.objects.filter(pk=root.pk).update(rank="bronze")
    root.refresh_from_db()
    _make_eligible(root)
    chain = root
    for _ in range(4):
        chain = _make_distributor(sponsor=chain)
    _make_binary_bonus_transaction(chain, Decimal("60"))  # level 4

    amount = process_matching_bonus_for_distributor(root, RUN_AT)

    assert amount == Decimal("0.00")


@pytest.mark.django_db
def test_silver_rank_reaches_a_level_4_plus_recruit_end_to_end():
    root = _make_distributor()
    Distributor.objects.filter(pk=root.pk).update(rank="silver")
    root.refresh_from_db()
    _make_eligible(root)
    chain = root
    for _ in range(4):
        chain = _make_distributor(sponsor=chain)
    _make_binary_bonus_transaction(chain, Decimal("60"))  # level 4

    amount = process_matching_bonus_for_distributor(root, RUN_AT)

    assert amount == Decimal("3.00")  # 5% of 60


@pytest.mark.django_db
def test_a_bounded_silver_depth_setting_is_actually_read_not_ignored():
    """Regression guard (2026-07-22 code-review finding): a prior draft of
    _matching_bonus_depth_for_rank hardcoded Silver's depth as always-
    unlimited, ignoring config.MATCHING_BONUS_DEPTH_SILVER entirely -- the
    same 'decorative constance setting' bug class already found once for
    BINARY_BONUS_INTERVAL_MINUTES. Every other Silver test in this file
    only exercises the seeded default (0 = unlimited), which can't tell
    'reads the live setting' apart from 'always returns unlimited' -- this
    sets a real nonzero value and proves a level past it is excluded,
    exactly like Bronze's own depth-cap test does."""
    original_depth = config.MATCHING_BONUS_DEPTH_SILVER
    config.MATCHING_BONUS_DEPTH_SILVER = 2
    try:
        root = _make_distributor()
        Distributor.objects.filter(pk=root.pk).update(rank="silver")
        root.refresh_from_db()
        _make_eligible(root)
        chain = root
        for _ in range(3):
            chain = _make_distributor(sponsor=chain)
        _make_binary_bonus_transaction(chain, Decimal("60"))  # level 3

        amount = process_matching_bonus_for_distributor(root, RUN_AT)

        assert amount == Decimal("0.00")
    finally:
        config.MATCHING_BONUS_DEPTH_SILVER = original_depth


@pytest.mark.django_db
def test_zero_downline_earnings_credits_nothing():
    root = _make_distributor()
    Distributor.objects.filter(pk=root.pk).update(rank="bronze")
    root.refresh_from_db()
    _make_eligible(root)

    amount = process_matching_bonus_for_distributor(root, RUN_AT)

    assert amount == Decimal("0.00")
    assert not Wallet.objects.filter(distributor=root, balance__gt=0).exists()


@pytest.mark.django_db
def test_rerunning_with_the_same_run_at_is_idempotent():
    root = _make_distributor()
    Distributor.objects.filter(pk=root.pk).update(rank="bronze")
    root.refresh_from_db()
    _make_eligible(root)
    level1 = _make_distributor(sponsor=root)
    _make_binary_bonus_transaction(level1, Decimal("200"))

    first = process_matching_bonus_for_distributor(root, RUN_AT)
    second = process_matching_bonus_for_distributor(root, RUN_AT)

    assert first == Decimal("10.00")
    assert second == Decimal("10.00")
    assert _wallet_balance(root) == Decimal("10.00")
    assert (
        WalletTransaction.objects.filter(
            transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS
        ).count()
        == 1
    )


@pytest.mark.django_db
@patch("apps.commissions.services.timezone.now")
def test_eligibility_uses_run_at_not_real_now(mock_now):
    mock_now.return_value = datetime(2099, 1, 1, tzinfo=dt_timezone.utc)
    root = _make_distributor()
    Distributor.objects.filter(pk=root.pk).update(rank="bronze")
    root.refresh_from_db()
    _make_eligible(root)  # eligible for RUN_AT's month (2020-03), not 2099-01
    level1 = _make_distributor(sponsor=root)
    _make_binary_bonus_transaction(level1, Decimal("200"))

    amount = process_matching_bonus_for_distributor(root, RUN_AT)

    assert amount == Decimal("10.00")


@pytest.mark.django_db
def test_works_correctly_when_called_with_an_unsaved_stub_like_the_batch_driver_does():
    """Regression guard (2026-07-22 doubt-driven-development review): the
    Celery Beat batch driver calls this with an unsaved Distributor(pk=id)
    stub, exactly like calculate_binary_bonus already does for
    process_binary_bonus_for_distributor (which only ever needs .pk). An
    earlier draft of this function discarded its own locked-row fetch and
    read .rank off that stub instead -- a stub's rank is always "" (the
    model's default), which resolves to 'no matching bonus tier' with no
    exception raised anywhere. That bug would have silently paid nobody,
    forever, and every other test in this file (which all pass a real,
    DB-fetched Distributor) would never have caught it."""
    root = _make_distributor()
    Distributor.objects.filter(pk=root.pk).update(rank="bronze")
    _make_eligible(root)
    level1 = _make_distributor(sponsor=root)
    _make_binary_bonus_transaction(level1, Decimal("200"))

    stub = Distributor(pk=root.pk)  # deliberately NOT root -- unsaved, blank rank
    amount = process_matching_bonus_for_distributor(stub, RUN_AT)

    assert amount == Decimal("10.00")
