from django.contrib.auth import get_user_model
from django.db import IntegrityError

import pytest

from apps.binary_tree.models import BinaryTreeEdge
from apps.distributors.models import Distributor
from apps.pv_ledger.models import PvLedger

User = get_user_model()


def _make_distributor(username, phone):
    user = User.objects.create_user(username=username, password="testpass123")
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db
def test_creating_an_edge_for_a_single_parent_child_pair_stores_the_expected_row():
    parent = _make_distributor("parent", "+233200000001")
    child = _make_distributor("child", "+233200000002")

    edge = BinaryTreeEdge.objects.create(
        ancestor=parent, descendant=child, depth=1, leg=BinaryTreeEdge.Leg.LEFT
    )

    assert edge.ancestor == parent
    assert edge.descendant == child
    assert edge.depth == 1
    assert edge.leg == BinaryTreeEdge.Leg.LEFT


@pytest.mark.django_db
def test_edge_leg_choices_are_left_or_right():
    parent = _make_distributor("parent", "+233200000003")
    child = _make_distributor("child", "+233200000004")

    left_edge = BinaryTreeEdge.objects.create(
        ancestor=parent, descendant=child, depth=1, leg=BinaryTreeEdge.Leg.LEFT
    )
    assert left_edge.leg == "L"

    other_child = _make_distributor("other_child", "+233200000005")
    right_edge = BinaryTreeEdge.objects.create(
        ancestor=parent, descendant=other_child, depth=1, leg=BinaryTreeEdge.Leg.RIGHT
    )
    assert right_edge.leg == "R"


@pytest.mark.django_db
def test_duplicate_ancestor_descendant_pair_is_rejected():
    parent = _make_distributor("parent", "+233200000006")
    child = _make_distributor("child", "+233200000007")
    BinaryTreeEdge.objects.create(
        ancestor=parent, descendant=child, depth=1, leg=BinaryTreeEdge.Leg.LEFT
    )

    with pytest.raises(IntegrityError):
        BinaryTreeEdge.objects.create(
            ancestor=parent, descendant=child, depth=1, leg=BinaryTreeEdge.Leg.RIGHT
        )


@pytest.mark.django_db
def test_multi_depth_edges_for_a_grandparent_pair():
    grandparent = _make_distributor("grandparent", "+233200000008")
    parent = _make_distributor("parent2", "+233200000009")
    child = _make_distributor("child2", "+233200000010")

    BinaryTreeEdge.objects.create(
        ancestor=grandparent, descendant=parent, depth=1, leg=BinaryTreeEdge.Leg.LEFT
    )
    BinaryTreeEdge.objects.create(
        ancestor=parent, descendant=child, depth=1, leg=BinaryTreeEdge.Leg.RIGHT
    )
    grandparent_edge = BinaryTreeEdge.objects.create(
        ancestor=grandparent, descendant=child, depth=2, leg=BinaryTreeEdge.Leg.LEFT
    )

    assert grandparent_edge.depth == 2
    assert (
        BinaryTreeEdge.objects.filter(descendant=child).count() == 2
    )  # direct parent edge + grandparent edge


@pytest.mark.django_db
def test_edge_cannot_reference_a_distributor_as_its_own_ancestor():
    solo = _make_distributor("solo3", "+233200000013")

    with pytest.raises(IntegrityError):
        BinaryTreeEdge.objects.create(
            ancestor=solo, descendant=solo, depth=1, leg=BinaryTreeEdge.Leg.LEFT
        )


@pytest.mark.django_db
def test_edge_depth_must_be_at_least_one():
    parent = _make_distributor("parent3", "+233200000014")
    child = _make_distributor("child3", "+233200000015")

    with pytest.raises(IntegrityError):
        BinaryTreeEdge.objects.create(
            ancestor=parent, descendant=child, depth=0, leg=BinaryTreeEdge.Leg.LEFT
        )


@pytest.mark.django_db
def test_pv_ledger_defaults_both_legs_to_zero():
    distributor = _make_distributor("solo", "+233200000011")

    ledger = PvLedger.objects.create(distributor=distributor)

    assert ledger.left_leg_pv == 0
    assert ledger.right_leg_pv == 0


@pytest.mark.django_db
def test_pv_ledger_is_one_per_distributor():
    distributor = _make_distributor("solo2", "+233200000012")
    PvLedger.objects.create(distributor=distributor)

    with pytest.raises(IntegrityError):
        PvLedger.objects.create(distributor=distributor)
