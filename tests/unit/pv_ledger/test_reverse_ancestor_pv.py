from datetime import date
from itertools import count

from django.contrib.auth import get_user_model

import pytest

from apps.binary_tree.models import BinaryTreeEdge
from apps.binary_tree.services import BinaryTree
from apps.distributors.models import Distributor
from apps.pv_ledger.models import MonthlyPersonalPv, PvDailyBucket, PvLedger
from apps.pv_ledger.services import (
    record_personal_pv,
    record_purchase_pv,
    reverse_ancestor_pv,
)

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233243{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db
def test_reverses_ledger_daily_bucket_and_personal_pv():
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)
    purchase_date = date(2026, 6, 1)

    record_purchase_pv(distributor, 60, today=purchase_date)
    record_personal_pv(distributor, 60, today=purchase_date)

    ledger = PvLedger.objects.get(distributor=sponsor)
    assert ledger.right_leg_pv == 60
    bucket = PvDailyBucket.objects.get(distributor=sponsor)
    assert bucket.pv == 60
    personal = MonthlyPersonalPv.objects.get(distributor=distributor)
    assert personal.pv == 60

    reverse_ancestor_pv(distributor, 60, purchase_date)

    ledger.refresh_from_db()
    bucket.refresh_from_db()
    personal.refresh_from_db()
    assert ledger.right_leg_pv == 0
    assert bucket.pv == 0
    assert personal.pv == 0


@pytest.mark.django_db
def test_targets_the_exact_purchase_date_bucket_not_todays():
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.LEFT)
    purchase_date = date(2026, 5, 3)

    record_purchase_pv(distributor, 60, today=purchase_date)
    record_personal_pv(distributor, 60, today=purchase_date)

    reverse_ancestor_pv(distributor, 60, purchase_date)

    bucket = PvDailyBucket.objects.get(distributor=sponsor)
    assert bucket.pv == 0
    assert bucket.date == purchase_date
    assert PvDailyBucket.objects.filter(distributor=sponsor).count() == 1
    personal = MonthlyPersonalPv.objects.get(distributor=distributor)
    assert personal.pv == 0
    assert personal.period == date(2026, 5, 1)


@pytest.mark.django_db
def test_a_root_distributor_still_gets_their_own_personal_pv_reversed():
    """Regression test (CodeRabbit finding on PR #35, confirmed real): the
    original implementation early-`return`ed when `edges` was empty (a
    root distributor, or one not yet placed under a sponsor) before ever
    reaching the MonthlyPersonalPv reversal below -- silently leaving a
    root distributor's own personal PV permanently inflated after any
    cancellation/refund. The original version of this test only asserted
    "no exception," which is why it didn't catch the bug."""
    distributor = _make_distributor()
    BinaryTree.place_distributor(None, distributor, leg=None)
    purchase_date = date(2026, 6, 1)
    record_personal_pv(distributor, 60, today=purchase_date)
    personal = MonthlyPersonalPv.objects.get(distributor=distributor)
    assert personal.pv == 60

    reverse_ancestor_pv(distributor, 60, purchase_date)  # must not raise

    personal.refresh_from_db()
    assert personal.pv == 0


@pytest.mark.django_db
def test_a_partially_consumed_daily_bucket_is_reversed_to_zero_not_negative():
    """PvDailyBucket has a real pv__gte=0 CheckConstraint -- a shortfall
    (e.g. already consumed by a Binary Bonus payout) must be logged as an
    accepted gap, never driven negative."""
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)
    purchase_date = date(2026, 6, 1)
    record_purchase_pv(distributor, 60, today=purchase_date)

    bucket = PvDailyBucket.objects.get(distributor=sponsor)
    bucket.pv = 10  # Simulate partial consumption by a Binary Bonus payout.
    bucket.save(update_fields=["pv"])

    reverse_ancestor_pv(distributor, 60, purchase_date)

    bucket.refresh_from_db()
    assert bucket.pv == 10  # Untouched -- can't reverse below what's live.


@pytest.mark.django_db
def test_pv_amount_must_be_positive():
    distributor = _make_distributor()
    with pytest.raises(ValueError):
        reverse_ancestor_pv(distributor, 0, date(2026, 6, 1))
    with pytest.raises(ValueError):
        reverse_ancestor_pv(distributor, -5, date(2026, 6, 1))
