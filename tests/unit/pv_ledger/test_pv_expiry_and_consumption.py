from datetime import date, timedelta
from itertools import count

from django.contrib.auth import get_user_model

import pytest

from apps.binary_tree.models import BinaryTreeEdge
from apps.distributors.models import Distributor
from apps.pv_ledger.models import PvDailyBucket
from apps.pv_ledger.services import (
    consume_leg_pv_fifo,
    distributor_ids_with_pending_pv,
    expire_old_pv,
    sum_leg_pv,
)

User = get_user_model()
_phone_seq = count(1)

CUTOFF = date(2026, 7, 15) - timedelta(days=180)


def _make_distributor():
    phone = f"+233246{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _bucket(distributor, leg, d, pv):
    return PvDailyBucket.objects.create(distributor=distributor, leg=leg, date=d, pv=pv)


@pytest.mark.django_db
def test_sum_leg_pv_adds_up_all_non_expired_buckets_for_that_leg():
    d = _make_distributor()
    _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 6, 1), 100)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 7, 1), 50)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, date(2026, 7, 1), 999)

    assert sum_leg_pv(d, BinaryTreeEdge.Leg.LEFT, CUTOFF) == 150


@pytest.mark.django_db
def test_sum_leg_pv_is_zero_not_null_when_the_leg_has_no_buckets():
    d = _make_distributor()

    assert sum_leg_pv(d, BinaryTreeEdge.Leg.LEFT, CUTOFF) == 0


@pytest.mark.django_db
def test_sum_leg_pv_excludes_buckets_older_than_cutoff():
    d = _make_distributor()
    _bucket(d, BinaryTreeEdge.Leg.LEFT, CUTOFF - timedelta(days=1), 100)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, CUTOFF, 40)

    assert sum_leg_pv(d, BinaryTreeEdge.Leg.LEFT, CUTOFF) == 40


@pytest.mark.django_db
def test_expire_old_pv_deletes_only_buckets_strictly_older_than_cutoff():
    """The boundary decision: a bucket dated exactly `cutoff_date` has
    survived its full PV_CARRY_FORWARD_EXPIRY_DAYS and is NOT expired --
    only strictly-older buckets are dropped."""
    d = _make_distributor()
    on_boundary = _bucket(d, BinaryTreeEdge.Leg.LEFT, CUTOFF, 40)
    one_day_past = _bucket(d, BinaryTreeEdge.Leg.LEFT, CUTOFF - timedelta(days=1), 25)

    expire_old_pv(d, CUTOFF)

    remaining_ids = set(PvDailyBucket.objects.values_list("pk", flat=True))
    assert remaining_ids == {on_boundary.pk}
    assert not PvDailyBucket.objects.filter(pk=one_day_past.pk).exists()


@pytest.mark.django_db
def test_expire_old_pv_is_a_no_op_when_nothing_is_expired():
    d = _make_distributor()
    _bucket(d, BinaryTreeEdge.Leg.LEFT, CUTOFF, 40)

    expire_old_pv(d, CUTOFF)

    assert PvDailyBucket.objects.count() == 1


@pytest.mark.django_db
def test_consume_leg_pv_fifo_takes_from_the_oldest_bucket_first():
    d = _make_distributor()
    older = _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 6, 1), 100)
    newer = _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 7, 1), 100)

    consume_leg_pv_fifo(d, BinaryTreeEdge.Leg.LEFT, CUTOFF, 60, "ref-1")

    older.refresh_from_db()
    newer.refresh_from_db()
    assert older.pv == 40
    assert newer.pv == 100


@pytest.mark.django_db
def test_consume_leg_pv_fifo_spans_multiple_buckets_when_the_oldest_is_not_enough():
    d = _make_distributor()
    oldest = _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 6, 1), 30)
    middle = _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 6, 15), 30)
    newest = _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 7, 1), 30)

    consume_leg_pv_fifo(d, BinaryTreeEdge.Leg.LEFT, CUTOFF, 50, "ref-1")

    oldest.refresh_from_db()
    middle.refresh_from_db()
    newest.refresh_from_db()
    assert oldest.pv == 0
    assert middle.pv == 10
    assert newest.pv == 30


@pytest.mark.django_db
def test_consume_leg_pv_fifo_consuming_exactly_the_total_leaves_zero_everywhere():
    d = _make_distributor()
    a = _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 6, 1), 30)
    b = _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 7, 1), 20)

    consume_leg_pv_fifo(d, BinaryTreeEdge.Leg.LEFT, CUTOFF, 50, "ref-1")

    a.refresh_from_db()
    b.refresh_from_db()
    assert a.pv == 0
    assert b.pv == 0


@pytest.mark.django_db
def test_consume_leg_pv_fifo_zero_amount_is_a_no_op():
    d = _make_distributor()
    a = _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 6, 1), 30)

    consume_leg_pv_fifo(d, BinaryTreeEdge.Leg.LEFT, CUTOFF, 0, "ref-1")

    a.refresh_from_db()
    assert a.pv == 30


@pytest.mark.django_db
def test_consume_leg_pv_fifo_raises_if_asked_to_consume_more_than_exists():
    d = _make_distributor()
    _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 6, 1), 30)

    with pytest.raises(RuntimeError):
        consume_leg_pv_fifo(d, BinaryTreeEdge.Leg.LEFT, CUTOFF, 100, "ref-1")


@pytest.mark.django_db
def test_consume_leg_pv_fifo_does_not_touch_the_other_leg():
    d = _make_distributor()
    left = _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 6, 1), 30)
    right = _bucket(d, BinaryTreeEdge.Leg.RIGHT, date(2026, 6, 1), 30)

    consume_leg_pv_fifo(d, BinaryTreeEdge.Leg.LEFT, CUTOFF, 30, "ref-1")

    left.refresh_from_db()
    right.refresh_from_db()
    assert left.pv == 0
    assert right.pv == 30


@pytest.mark.django_db
def test_distributor_ids_with_pending_pv_excludes_distributors_with_no_buckets():
    has_pv = _make_distributor()
    no_activity_at_all = _make_distributor()
    _bucket(has_pv, BinaryTreeEdge.Leg.LEFT, date(2026, 7, 1), 50)

    ids = distributor_ids_with_pending_pv()

    assert has_pv.pk in ids
    assert no_activity_at_all.pk not in ids


@pytest.mark.django_db
def test_distributor_ids_with_pending_pv_excludes_fully_consumed_buckets():
    """A bucket left at pv=0 by consume_leg_pv_fifo's partial-consumption
    boundary case still exists as a row -- but it has nothing left to
    pay out, so it shouldn't cost the batch driver a real evaluation."""
    d = _make_distributor()
    _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 7, 1), 0)

    ids = distributor_ids_with_pending_pv()

    assert d.pk not in ids


@pytest.mark.django_db
def test_distributor_ids_with_pending_pv_does_not_double_count_multiple_buckets():
    d = _make_distributor()
    _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 6, 1), 10)
    _bucket(d, BinaryTreeEdge.Leg.LEFT, date(2026, 7, 1), 20)
    _bucket(d, BinaryTreeEdge.Leg.RIGHT, date(2026, 7, 1), 5)

    ids = list(distributor_ids_with_pending_pv())

    assert ids.count(d.pk) == 1
