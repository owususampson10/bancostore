from datetime import date, timedelta
from itertools import count

from django.contrib.auth import get_user_model
from django.utils import timezone

import pytest

from apps.binary_tree.models import BinaryTreeEdge
from apps.distributors.models import Distributor
from apps.pv_ledger.models import PvDailyBucket
from apps.pv_ledger.services import get_carry_forward_summary

User = get_user_model()
_phone_seq = count(1)

# Matches config.py's seeded defaults (PV_CARRY_FORWARD_EXPIRY_DAYS=180,
# PV_EXPIRY_WARNING_DAYS=14) -- existing pv_ledger tests (e.g.
# test_pv_expiry_and_consumption.py) rely on these same defaults rather than
# overriding constance, so this file matches that convention.
EXPIRY_DAYS = 180
WARNING_DAYS = 14
TODAY = date(2026, 7, 15)
NOW = timezone.make_aware(timezone.datetime(2026, 7, 15, 12, 0, 0))


def _make_distributor():
    phone = f"+233247{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _bucket(distributor, leg, d, pv):
    return PvDailyBucket.objects.create(distributor=distributor, leg=leg, date=d, pv=pv)


@pytest.mark.django_db
def test_distributor_with_no_buckets_sees_an_honest_zero_state():
    d = _make_distributor()

    summary = get_carry_forward_summary(d, now=NOW)

    assert summary.pv == 0
    assert summary.leg is None
    assert summary.nearest_expiry_date is None
    assert summary.nearing_expiry is False


@pytest.mark.django_db
def test_strong_leg_is_the_one_with_more_non_expired_pv():
    d = _make_distributor()
    _bucket(d, BinaryTreeEdge.Leg.LEFT, TODAY - timedelta(days=10), 900)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, TODAY - timedelta(days=10), 300)

    summary = get_carry_forward_summary(d, now=NOW)

    assert summary.leg == BinaryTreeEdge.Leg.LEFT
    assert summary.pv == 900


@pytest.mark.django_db
def test_nearest_expiry_date_is_the_oldest_surviving_buckets_date_plus_expiry_window():
    d = _make_distributor()
    older = TODAY - timedelta(days=100)
    newer = TODAY - timedelta(days=10)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, older, 200)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, newer, 300)

    summary = get_carry_forward_summary(d, now=NOW)

    assert summary.pv == 500
    assert summary.nearest_expiry_date == older + timedelta(days=EXPIRY_DAYS)


@pytest.mark.django_db
def test_already_expired_buckets_are_excluded_from_total_and_expiry_date():
    d = _make_distributor()
    expired = TODAY - timedelta(days=EXPIRY_DAYS + 1)
    surviving = TODAY - timedelta(days=50)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, expired, 1000)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, surviving, 400)

    summary = get_carry_forward_summary(d, now=NOW)

    assert summary.pv == 400
    assert summary.nearest_expiry_date == surviving + timedelta(days=EXPIRY_DAYS)


@pytest.mark.django_db
def test_nearing_expiry_is_true_within_the_warning_window():
    d = _make_distributor()
    # Expires in WARNING_DAYS - 1 days from "now".
    bucket_date = TODAY - timedelta(days=EXPIRY_DAYS - (WARNING_DAYS - 1))
    _bucket(d, BinaryTreeEdge.Leg.LEFT, bucket_date, 100)

    summary = get_carry_forward_summary(d, now=NOW)

    assert summary.nearing_expiry is True


@pytest.mark.django_db
def test_nearing_expiry_is_false_outside_the_warning_window():
    d = _make_distributor()
    bucket_date = TODAY - timedelta(days=EXPIRY_DAYS - (WARNING_DAYS + 30))
    _bucket(d, BinaryTreeEdge.Leg.LEFT, bucket_date, 100)

    summary = get_carry_forward_summary(d, now=NOW)

    assert summary.nearing_expiry is False


@pytest.mark.django_db
def test_a_tie_between_legs_deterministically_picks_left():
    d = _make_distributor()
    _bucket(d, BinaryTreeEdge.Leg.LEFT, TODAY - timedelta(days=10), 500)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, TODAY - timedelta(days=10), 500)

    summary = get_carry_forward_summary(d, now=NOW)

    assert summary.leg == BinaryTreeEdge.Leg.LEFT
    assert summary.pv == 500
