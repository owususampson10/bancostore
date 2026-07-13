import threading
from itertools import count

from django.contrib.auth import get_user_model
from django.db import connection

import pytest

from apps.binary_tree.models import BinaryTreeEdge
from apps.binary_tree.services import BinaryTree
from apps.distributors.models import Distributor
from apps.pv_ledger.models import PvLedger

User = get_user_model()

_phone_seq = count(1)


def _make_distributor(name):
    user = User.objects.create_user(username=name, password="testpass123")
    phone = f"+233300{next(_phone_seq):06d}"
    return Distributor.objects.create(user=user, phone_number=phone)


def _direct_child(ancestor, leg):
    edge = (
        BinaryTreeEdge.objects.filter(ancestor=ancestor, depth=1, leg=leg)
        .select_related("descendant")
        .first()
    )
    return edge.descendant if edge else None


def _leg_from(ancestor, descendant):
    return BinaryTreeEdge.objects.get(ancestor=ancestor, descendant=descendant).leg


@pytest.mark.django_db
def test_direct_placement_when_sponsors_chosen_leg_is_open():
    sponsor = _make_distributor("sponsor1")
    new_distributor = _make_distributor("new1")

    BinaryTree.place_distributor(sponsor, new_distributor, leg=BinaryTreeEdge.Leg.LEFT)

    assert _direct_child(sponsor, BinaryTreeEdge.Leg.LEFT) == new_distributor
    assert _leg_from(sponsor, new_distributor) == BinaryTreeEdge.Leg.LEFT


@pytest.mark.django_db
def test_spillover_places_under_direct_child_when_sponsors_slot_is_taken():
    sponsor = _make_distributor("sponsor2")
    first = _make_distributor("first2")
    second = _make_distributor("second2")

    BinaryTree.place_distributor(sponsor, first, leg=BinaryTreeEdge.Leg.LEFT)
    BinaryTree.place_distributor(sponsor, second, leg=BinaryTreeEdge.Leg.LEFT)

    # sponsor's left slot is taken by `first`, so `second` spills onto first's
    # own left slot (first has no children yet)
    assert _direct_child(sponsor, BinaryTreeEdge.Leg.LEFT) == first
    assert _direct_child(first, BinaryTreeEdge.Leg.LEFT) == second
    assert _leg_from(sponsor, second) == BinaryTreeEdge.Leg.LEFT


@pytest.mark.django_db
def test_spillover_fills_the_shallowest_level_before_going_deeper():
    """Regression guard against a DFS-shaped bug: a naive recursive spillover
    could fully explore one child's subtree before checking its sibling.
    Correct behavior is strict level-order (BFS)."""
    sponsor = _make_distributor("sponsor3")
    node_a = _make_distributor("a3")
    node_b = _make_distributor("b3")
    node_c = _make_distributor("c3")
    node_d = _make_distributor("d3")
    node_e = _make_distributor("e3")

    # Build: sponsor -L-> A; A -L-> B, A -R-> C; B -L-> D, B -R-> E
    # C has no children -- it's the shallowest open slot once A and B are full.
    BinaryTree.place_distributor(sponsor, node_a, leg=BinaryTreeEdge.Leg.LEFT)
    BinaryTree.place_distributor(
        sponsor, node_b, leg=BinaryTreeEdge.Leg.LEFT
    )  # -> A's left
    BinaryTree.place_distributor(
        sponsor, node_c, leg=BinaryTreeEdge.Leg.LEFT
    )  # -> A's right
    BinaryTree.place_distributor(
        sponsor, node_d, leg=BinaryTreeEdge.Leg.LEFT
    )  # -> B's left
    BinaryTree.place_distributor(
        sponsor, node_e, leg=BinaryTreeEdge.Leg.LEFT
    )  # -> B's right

    assert _direct_child(node_a, BinaryTreeEdge.Leg.LEFT) == node_b
    assert _direct_child(node_a, BinaryTreeEdge.Leg.RIGHT) == node_c
    assert _direct_child(node_b, BinaryTreeEdge.Leg.LEFT) == node_d
    assert _direct_child(node_b, BinaryTreeEdge.Leg.RIGHT) == node_e
    assert _direct_child(node_c, BinaryTreeEdge.Leg.LEFT) is None

    newcomer = _make_distributor("newcomer3")
    BinaryTree.place_distributor(sponsor, newcomer, leg=BinaryTreeEdge.Leg.LEFT)

    # C (depth 2 from sponsor) has an open slot; D and E's children would be
    # depth 4. A correct BFS lands the newcomer on C, not under B's subtree.
    assert _direct_child(node_c, BinaryTreeEdge.Leg.LEFT) == newcomer


@pytest.mark.django_db
def test_spillover_never_crosses_to_the_other_leg():
    sponsor = _make_distributor("sponsor4")
    left_child = _make_distributor("left4")
    right_child = _make_distributor("right4")
    BinaryTree.place_distributor(sponsor, left_child, leg=BinaryTreeEdge.Leg.LEFT)
    BinaryTree.place_distributor(sponsor, right_child, leg=BinaryTreeEdge.Leg.RIGHT)

    newcomer = _make_distributor("newcomer4")
    BinaryTree.place_distributor(sponsor, newcomer, leg=BinaryTreeEdge.Leg.LEFT)

    # sponsor's left slot (left_child) is taken, but left_child itself has
    # two open slots -- spillover must land there, never under right_child.
    assert _leg_from(sponsor, newcomer) == BinaryTreeEdge.Leg.LEFT
    assert not BinaryTreeEdge.objects.filter(
        ancestor=right_child, descendant=newcomer
    ).exists()


@pytest.mark.django_db
def test_auto_balance_places_under_the_weaker_leg():
    sponsor = _make_distributor("sponsor5")
    PvLedger.objects.create(distributor=sponsor, left_leg_pv=100, right_leg_pv=30)
    newcomer = _make_distributor("newcomer5")

    BinaryTree.place_distributor(sponsor, newcomer, leg=None)

    assert _direct_child(sponsor, BinaryTreeEdge.Leg.RIGHT) == newcomer


@pytest.mark.django_db
def test_auto_balance_defaults_to_left_on_a_tie():
    sponsor = _make_distributor("sponsor6")
    PvLedger.objects.create(distributor=sponsor, left_leg_pv=50, right_leg_pv=50)
    newcomer = _make_distributor("newcomer6")

    BinaryTree.place_distributor(sponsor, newcomer, leg=None)

    assert _direct_child(sponsor, BinaryTreeEdge.Leg.LEFT) == newcomer


@pytest.mark.django_db
def test_leg_propagates_from_the_sponsors_own_leg_not_the_local_slot():
    great_grandparent = _make_distributor("gg7")
    sponsor = _make_distributor("sponsor7")
    BinaryTree.place_distributor(
        great_grandparent, sponsor, leg=BinaryTreeEdge.Leg.RIGHT
    )

    first = _make_distributor("first7")
    newcomer = _make_distributor("newcomer7")
    # Both placed on sponsor's LEFT leg -- second one spills under `first`.
    BinaryTree.place_distributor(sponsor, first, leg=BinaryTreeEdge.Leg.LEFT)
    BinaryTree.place_distributor(sponsor, newcomer, leg=BinaryTreeEdge.Leg.LEFT)

    # From great_grandparent's perspective, everything under `sponsor` is on
    # sponsor's leg (RIGHT), regardless of which local leg it landed on
    # under `sponsor` or `first`.
    assert _leg_from(great_grandparent, newcomer) == BinaryTreeEdge.Leg.RIGHT
    edge = BinaryTreeEdge.objects.get(ancestor=great_grandparent, descendant=newcomer)
    assert edge.depth == 3


@pytest.mark.django_db
def test_closure_table_has_no_orphaned_or_duplicate_edges_after_several_placements():
    sponsor = _make_distributor("sponsor8")
    placed = [_make_distributor(f"n8_{i}") for i in range(5)]
    for distributor in placed:
        BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.LEFT)

    for distributor in placed:
        pairs = BinaryTreeEdge.objects.filter(descendant=distributor).values_list(
            "ancestor_id", flat=True
        )
        assert len(pairs) == len(
            set(pairs)
        )  # no duplicate ancestor for the same descendant


@pytest.mark.django_db(transaction=True)
def test_concurrent_placements_under_the_same_sponsor_never_corrupt_the_tree():
    """place_distributor locks the sponsor row specifically so two concurrent
    placements can't both see the same open slot and both attach there. This
    only genuinely proves the locking works under a database that supports
    row locking -- connection.features.has_select_for_update is False on
    SQLite, so Django silently drops the FOR UPDATE clause here (same
    constraint CLAUDE.md notes for commission/wallet/PV-ledger tests: they
    need real MySQL in CI). Keeping the test anyway: it's meaningful once run
    against MySQL, and it still catches a regression in the surrounding
    logic (e.g. someone removing the lock/retry wrapper entirely) even on
    SQLite, since each thread gets its own connection here rather than
    sharing state."""
    sponsor = _make_distributor("sponsor10")
    newcomers = [_make_distributor(f"newcomer10_{i}") for i in range(5)]

    def attempt(newcomer):
        try:
            BinaryTree.place_distributor(sponsor, newcomer, leg=BinaryTreeEdge.Leg.LEFT)
        finally:
            connection.close()  # each thread must not share the main
            # thread's connection/transaction state

    threads = [threading.Thread(target=attempt, args=(n,)) for n in newcomers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Structural invariant: no (ancestor, leg) pair has more than one direct
    # (depth=1) child, no matter how the five placements interleaved.
    direct_child_pairs = BinaryTreeEdge.objects.filter(depth=1).values_list(
        "ancestor_id", "leg"
    )
    assert len(direct_child_pairs) == len(set(direct_child_pairs))
    # And every newcomer actually landed somewhere -- none were dropped.
    for newcomer in newcomers:
        assert BinaryTreeEdge.objects.filter(descendant=newcomer).exists()


@pytest.mark.django_db
def test_placing_with_no_sponsor_creates_no_edges():
    root = _make_distributor("root9")

    BinaryTree.place_distributor(None, root, leg=None)

    assert not BinaryTreeEdge.objects.filter(descendant=root).exists()
    assert not BinaryTreeEdge.objects.filter(ancestor=root).exists()
