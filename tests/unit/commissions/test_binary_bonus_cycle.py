import threading
import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection

import pytest
from constance import config

import apps.commissions.services as commissions_services
from apps.binary_tree.models import BinaryTreeEdge
from apps.commissions.services import process_binary_bonus_for_distributor
from apps.distributors.models import Distributor
from apps.pv_ledger.models import MonthlyPersonalPv, PvDailyBucket
from apps.pv_ledger.services import _credit_daily_buckets
from apps.wallet.models import Wallet, WalletTransaction
from bancostore.concurrency import retry_on_lock_contention

User = get_user_model()
_phone_seq = count(1)

RUN_AT = datetime(2020, 3, 10, 10, 0, tzinfo=dt_timezone.utc)
CUTOFF = RUN_AT.date() - timedelta(days=180)
# Deliberately NOT today's real date -- a regression that reads real
# timezone.now() instead of the passed run_at must be able to fail a
# test, which it can't if RUN_AT happens to equal the sandbox's actual
# current date (a real gap a fresh review caught in this exact file).


def _make_distributor():
    phone = f"+233247{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _make_eligible(distributor, run_at=RUN_AT):
    MonthlyPersonalPv.objects.create(
        distributor=distributor,
        period=run_at.date().replace(day=1),
        pv=config.MIN_MONTHLY_PERSONAL_PV,
    )


def _bucket(distributor, leg, d, pv):
    return PvDailyBucket.objects.create(distributor=distributor, leg=leg, date=d, pv=pv)


def _wallet_balance(distributor):
    return Wallet.objects.get(distributor=distributor).balance


@pytest.mark.django_db
def test_reproduces_the_doc_example_1500_left_600_right():
    """left 1,500 / right 600 -> weak leg 600 -> GHS 45.00 bonus, 900 PV
    carried forward on the strong (left) leg, weak (right) leg emptied."""
    d = _make_distributor()
    _make_eligible(d)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, RUN_AT.date(), 600)

    amount = process_binary_bonus_for_distributor(d, RUN_AT)

    assert amount == Decimal("45.00")
    assert _wallet_balance(d) == Decimal("45.00")
    left_total = PvDailyBucket.objects.get(
        distributor=d, leg=BinaryTreeEdge.Leg.LEFT
    ).pv
    right_total = PvDailyBucket.objects.get(
        distributor=d, leg=BinaryTreeEdge.Leg.RIGHT
    ).pv
    assert left_total == 900
    assert right_total == 0


@pytest.mark.django_db
def test_ineligible_distributor_gets_no_bonus_and_no_consumption():
    d = _make_distributor()
    # No MonthlyPersonalPv row created -- not eligible.
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, RUN_AT.date(), 600)

    amount = process_binary_bonus_for_distributor(d, RUN_AT)

    assert amount == Decimal("0.00")
    assert not Wallet.objects.filter(distributor=d).exists()
    assert (
        PvDailyBucket.objects.get(distributor=d, leg=BinaryTreeEdge.Leg.RIGHT).pv == 600
    )


@pytest.mark.django_db
def test_an_ineligible_distributors_expired_pv_still_gets_cleaned_up():
    """Expiry must run unconditionally, before the eligibility check --
    otherwise a chronically-ineligible distributor's buckets would
    accumulate forever, since they'd never reach the code path that
    prunes them."""
    d = _make_distributor()
    # No MonthlyPersonalPv row -- not eligible.
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, CUTOFF - timedelta(days=1), 600)  # expired

    amount = process_binary_bonus_for_distributor(d, RUN_AT)

    assert amount == Decimal("0.00")
    assert not PvDailyBucket.objects.filter(distributor=d).exists()


@pytest.mark.django_db
def test_zero_weak_leg_produces_no_bonus():
    d = _make_distributor()
    _make_eligible(d)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    # Right leg has nothing.

    amount = process_binary_bonus_for_distributor(d, RUN_AT)

    assert amount == Decimal("0.00")
    assert not WalletTransaction.objects.filter(
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS
    ).exists()


@pytest.mark.django_db
def test_weekly_cap_reduces_payout_and_consumes_only_the_proportional_pv():
    d = _make_distributor()
    _make_eligible(d)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, RUN_AT.date(), 600)
    # Pre-fill the weekly cap so only GHS 10 of room remains (raw bonus
    # would be GHS 45).
    already_paid = config.WEEKLY_BINARY_BONUS_CAP - Decimal("10.00")
    wallet = Wallet.objects.create(distributor=d, balance=already_paid)
    WalletTransaction.objects.create(
        wallet=wallet,
        amount=already_paid,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="prior-cycle",
    )

    amount = process_binary_bonus_for_distributor(d, RUN_AT)

    assert amount == Decimal("10.00")
    # 10.00 / 45.00 of the 600 weak-leg PV = 133 (floored, never rounded up).
    expected_pv_consumed = int(600 * Decimal("10.00") / Decimal("45.00"))
    right_total = PvDailyBucket.objects.get(
        distributor=d, leg=BinaryTreeEdge.Leg.RIGHT
    ).pv
    assert right_total == 600 - expected_pv_consumed
    left_total = PvDailyBucket.objects.get(
        distributor=d, leg=BinaryTreeEdge.Leg.LEFT
    ).pv
    assert left_total == 1500 - expected_pv_consumed


@pytest.mark.django_db
def test_already_at_the_cap_pays_and_consumes_nothing():
    d = _make_distributor()
    _make_eligible(d)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, RUN_AT.date(), 600)
    wallet = Wallet.objects.create(
        distributor=d, balance=config.WEEKLY_BINARY_BONUS_CAP
    )
    WalletTransaction.objects.create(
        wallet=wallet,
        amount=config.WEEKLY_BINARY_BONUS_CAP,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="prior-cycle",
    )

    amount = process_binary_bonus_for_distributor(d, RUN_AT)

    assert amount == Decimal("0.00")
    assert (
        PvDailyBucket.objects.get(distributor=d, leg=BinaryTreeEdge.Leg.RIGHT).pv == 600
    )


@pytest.mark.django_db
def test_an_owed_bonus_too_small_to_cover_1_pv_is_deferred_and_logged(caplog):
    """When the cap leaves only a sliver of room (nonzero actual_bonus,
    but too small relative to raw_bonus to floor to even 1 PV), nothing
    is paid and nothing is consumed THIS cycle -- paying it now would
    leave the weak leg's PV un-marked-as-spent, letting further slivers
    accrue against the same PV in later cycles (a real double-pay risk).
    This deferral is logged so it's distinguishable from "nothing owed"."""
    import logging

    d = _make_distributor()
    _make_eligible(d)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, RUN_AT.date(), 600)
    # Raw bonus would be GHS 45.00; leave only GHS 0.01 of cap room, far
    # too little to floor to 1 PV of the 600 weak-leg total.
    already_paid = config.WEEKLY_BINARY_BONUS_CAP - Decimal("0.01")
    wallet = Wallet.objects.create(distributor=d, balance=already_paid)
    WalletTransaction.objects.create(
        wallet=wallet,
        amount=already_paid,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="prior-cycle",
    )

    with caplog.at_level(logging.INFO):
        amount = process_binary_bonus_for_distributor(d, RUN_AT)

    assert amount == Decimal("0.00")
    assert _wallet_balance(d) == already_paid  # unchanged -- nothing newly credited
    assert (
        PvDailyBucket.objects.get(distributor=d, leg=BinaryTreeEdge.Leg.RIGHT).pv == 600
    )
    assert "floors to 0 PV" in caplog.text


@pytest.mark.django_db
def test_expired_pv_is_dropped_before_being_counted_or_paid():
    d = _make_distributor()
    _make_eligible(d)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, CUTOFF - timedelta(days=1), 600)  # expired

    amount = process_binary_bonus_for_distributor(d, RUN_AT)

    assert amount == Decimal("0.00")
    assert not PvDailyBucket.objects.filter(
        distributor=d, leg=BinaryTreeEdge.Leg.RIGHT
    ).exists()


@pytest.mark.django_db
def test_pv_dated_exactly_on_the_expiry_boundary_still_counts():
    d = _make_distributor()
    _make_eligible(d)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, CUTOFF, 600)  # exactly on the boundary

    amount = process_binary_bonus_for_distributor(d, RUN_AT)

    assert amount == Decimal("45.00")


@pytest.mark.django_db
@patch("apps.pv_ledger.services.timezone.now")
def test_eligibility_uses_run_at_not_real_now(mock_now):
    """Regression guard: process_binary_bonus_for_distributor must pass
    `now=run_at` through to is_eligible_for_binary_bonus explicitly. If a
    future change drops that kwarg, is_eligible_for_binary_bonus falls
    back to real timezone.now() -- mocked here to a wildly different
    month with no MonthlyPersonalPv row, which would flip this
    distributor to ineligible and fail this test."""
    mock_now.return_value = datetime(2099, 1, 1, tzinfo=dt_timezone.utc)
    d = _make_distributor()
    _make_eligible(d)  # eligible for RUN_AT's month (2020-03), not 2099-01
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, RUN_AT.date(), 600)

    amount = process_binary_bonus_for_distributor(d, RUN_AT)

    assert amount == Decimal("45.00")


@pytest.mark.django_db
@patch("apps.commissions.services.timezone.now")
def test_weekly_cap_uses_run_at_not_real_now(mock_now):
    """Same regression guard as above, for apply_weekly_binary_bonus_cap:
    a prior BINARY_BONUS payment is backdated to fall inside RUN_AT's
    rolling 7-day window but nowhere near the mocked real "now"'s. If a
    future change drops `now=run_at` from the cap call, the cutoff would
    be computed from the mocked value instead, the prior payment would
    fall outside it, and the cap would incorrectly allow the full raw
    bonus through instead of the correctly-reduced amount."""
    mock_now.return_value = datetime(2099, 1, 1, tzinfo=dt_timezone.utc)
    d = _make_distributor()
    _make_eligible(d)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, RUN_AT.date(), 600)
    already_paid = config.WEEKLY_BINARY_BONUS_CAP - Decimal("10.00")
    wallet = Wallet.objects.create(distributor=d, balance=already_paid)
    txn = WalletTransaction.objects.create(
        wallet=wallet,
        amount=already_paid,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="prior-cycle",
    )
    WalletTransaction.objects.filter(pk=txn.pk).update(
        created_at=RUN_AT - timedelta(days=3)
    )

    amount = process_binary_bonus_for_distributor(d, RUN_AT)

    assert amount == Decimal("10.00")


@pytest.mark.django_db
def test_rerunning_with_the_same_run_at_is_idempotent():
    d = _make_distributor()
    _make_eligible(d)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, RUN_AT.date(), 600)

    first = process_binary_bonus_for_distributor(d, RUN_AT)
    second = process_binary_bonus_for_distributor(d, RUN_AT)

    assert first == Decimal("45.00")
    assert second == Decimal("45.00")
    assert _wallet_balance(d) == Decimal("45.00")
    assert (
        WalletTransaction.objects.filter(
            transaction_type=WalletTransaction.TransactionType.BINARY_BONUS
        ).count()
        == 1
    )
    assert (
        PvDailyBucket.objects.get(distributor=d, leg=BinaryTreeEdge.Leg.RIGHT).pv == 0
    )


@pytest.mark.django_db(transaction=True)
def test_a_concurrent_purchase_write_mid_cycle_never_loses_pv():
    """The design's core safety claim: PvDailyBucket.pv is only ever
    INCREASED outside this function (by the write-time purchase-credit
    path) and only ever DECREASED by this cycle -- so a concurrent
    purchase landing mid-cycle can only make MORE PV available than
    counted, never less, and consumption never needs to (or does) touch
    PV it didn't already count. Proven with a real thread rather than
    trusted from the math alone -- a prior bug in this exact codebase
    was only caught by a real concurrency test, not by reasoning.

    Precisely forces the risky window (a purchase attempting to land
    AFTER totals are read but BEFORE/DURING consumption) via a
    synchronization hook on sum_leg_pv's second call (the RIGHT leg, the
    weak leg here) -- without this, the two threads race freely and the
    purchase can just as easily land BEFORE the totals are read, which
    is a different, unremarkable case (it would simply be included in
    this cycle's count) and wouldn't actually exercise the invariant
    being tested.

    SQLite can't hold two overlapping write transactions at all (a
    whole-database lock, not per-row), so the cycle thread only pauses a
    short FIXED delay here rather than waiting for the purchase to
    signal completion -- waiting on each other would deadlock, exactly
    as it did in an earlier version of this style of test in this
    codebase (see test_daily_buckets.py's history). The purchase thread
    wraps its write in retry_on_lock_contention so it survives SQLite's
    "table is locked" until the cycle's transaction commits and releases
    it -- proving the logical property (a purchase attempted after the
    sum is never lost) even though SQLite can't prove the two writes
    were ever literally concurrent (only real MySQL can, per this
    project's established, already-documented limitation)."""
    d = _make_distributor()
    _make_eligible(d)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, RUN_AT.date(), 1500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, RUN_AT.date(), 600)

    totals_read = threading.Event()
    errors = []
    real_sum_leg_pv = commissions_services.sum_leg_pv

    def synced_sum_leg_pv(distributor, leg, cutoff_date):
        result = real_sum_leg_pv(distributor, leg, cutoff_date)
        if leg == BinaryTreeEdge.Leg.RIGHT:
            totals_read.set()
            time.sleep(0.3)
        return result

    cycle_result = {}

    def run_cycle():
        try:
            with patch.object(
                commissions_services, "sum_leg_pv", side_effect=synced_sum_leg_pv
            ):
                cycle_result["amount"] = process_binary_bonus_for_distributor(d, RUN_AT)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    def run_concurrent_purchase():
        totals_read.wait(timeout=5)
        # A purchase confirms strictly AFTER this cycle's totals were
        # read, crediting +50 PV to this same distributor's RIGHT leg
        # bucket for today -- PV that was never counted by this cycle's
        # totals and must survive intact.
        try:
            retry_on_lock_contention(
                lambda: _credit_daily_buckets(
                    [d.pk], BinaryTreeEdge.Leg.RIGHT, 50, RUN_AT.date()
                ),
                max_retries=30,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    t_cycle = threading.Thread(target=run_cycle)
    t_purchase = threading.Thread(target=run_concurrent_purchase)
    t_cycle.start()
    t_purchase.start()
    t_cycle.join(timeout=10)
    t_purchase.join(timeout=10)

    assert errors == []
    assert cycle_result["amount"] == Decimal("45.00")
    right_total = PvDailyBucket.objects.get(
        distributor=d, leg=BinaryTreeEdge.Leg.RIGHT
    ).pv
    # 600 credited, all consumed by the cycle (weak leg), PLUS the +50
    # the concurrent purchase added -- that +50 must never be lost.
    assert right_total == 50
