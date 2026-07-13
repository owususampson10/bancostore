from itertools import count

from django.contrib.auth import get_user_model

import pytest

from apps.binary_tree.models import BinaryTreeEdge
from apps.binary_tree.services import BinaryTree
from apps.distributors.models import Distributor
from apps.pv_ledger.models import PvLedger
from apps.pv_ledger.services import record_purchase_pv

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233242{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db
def test_credits_the_placed_leg_of_a_direct_ancestor():
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)

    record_purchase_pv(distributor, 500)

    ledger = PvLedger.objects.get(distributor=sponsor)
    assert ledger.right_leg_pv == 500
    assert ledger.left_leg_pv == 0


@pytest.mark.django_db
def test_zero_pv_is_a_no_op():
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.LEFT)

    record_purchase_pv(distributor, 0)

    ledger = PvLedger.objects.get(distributor=sponsor)
    assert ledger.left_leg_pv == 0


@pytest.mark.django_db
def test_no_ancestors_is_a_no_op():
    distributor = _make_distributor()
    PvLedger.objects.get_or_create(distributor=distributor)

    record_purchase_pv(distributor, 500)  # must not raise, no edges to walk


@pytest.mark.django_db
def test_missing_ancestor_ledger_logs_error_and_does_not_crash(caplog):
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.LEFT)
    PvLedger.objects.filter(distributor=sponsor).delete()

    record_purchase_pv(distributor, 500)  # must not raise

    assert "missing PvLedger row(s) for ancestor(s)" in caplog.text


@pytest.mark.django_db
def test_credits_multiple_ancestors_on_the_same_leg_in_one_bulk_update():
    grandparent = _make_distributor()
    parent = _make_distributor()
    BinaryTree.place_distributor(grandparent, parent, leg=BinaryTreeEdge.Leg.LEFT)
    distributor = _make_distributor()
    BinaryTree.place_distributor(parent, distributor, leg=BinaryTreeEdge.Leg.LEFT)

    record_purchase_pv(distributor, 500)

    assert PvLedger.objects.get(distributor=parent).left_leg_pv == 500
    assert PvLedger.objects.get(distributor=grandparent).left_leg_pv == 500
