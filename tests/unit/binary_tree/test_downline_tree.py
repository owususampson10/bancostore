from itertools import count

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from apps.binary_tree.models import BinaryTreeEdge
from apps.binary_tree.services import BinaryTree, get_downline_tree
from apps.distributors.models import Distributor
from apps.pv_ledger.services import record_purchase_pv

User = get_user_model()

_phone_seq = count(1)


def _make_distributor(name, **overrides):
    user = User.objects.create_user(username=name, password="testpass123")
    phone = f"+233301{next(_phone_seq):06d}"
    defaults = {"user": user, "phone_number": phone}
    defaults.update(overrides)
    return Distributor.objects.create(**defaults)


@pytest.mark.django_db
def test_a_distributor_with_no_downline_returns_just_themselves_with_no_children():
    root = _make_distributor("root1", full_name="Root One", ir_id="IR00001", rank="bronze")

    tree = get_downline_tree(root)

    assert tree.distributor_id == root.pk
    assert tree.full_name == "Root One"
    assert tree.ir_id == "IR00001"
    assert tree.rank == "bronze"
    assert tree.pv == 0
    assert tree.children == []


@pytest.mark.django_db
def test_direct_children_appear_with_correct_leg_and_data():
    root = _make_distributor("root2", full_name="Root Two", ir_id="IR00002", rank="silver")
    left_child = _make_distributor(
        "left2", full_name="Left Child", ir_id="IR00003", rank="bronze"
    )
    right_child = _make_distributor(
        "right2", full_name="Right Child", ir_id="IR00004", rank="bronze"
    )
    BinaryTree.place_distributor(root, left_child, leg=BinaryTreeEdge.Leg.LEFT)
    BinaryTree.place_distributor(root, right_child, leg=BinaryTreeEdge.Leg.RIGHT)

    tree = get_downline_tree(root)

    assert len(tree.children) == 2
    left_node = next(c for c in tree.children if c.leg == BinaryTreeEdge.Leg.LEFT)
    right_node = next(c for c in tree.children if c.leg == BinaryTreeEdge.Leg.RIGHT)
    assert left_node.full_name == "Left Child"
    assert left_node.ir_id == "IR00003"
    assert right_node.full_name == "Right Child"
    assert right_node.ir_id == "IR00004"


@pytest.mark.django_db
def test_multi_level_downline_nests_grandchildren_under_the_correct_child():
    root = _make_distributor("root3", full_name="Root Three", ir_id="IR00005")
    child = _make_distributor("child3", full_name="Child", ir_id="IR00006")
    grandchild = _make_distributor("grand3", full_name="Grandchild", ir_id="IR00007")
    BinaryTree.place_distributor(root, child, leg=BinaryTreeEdge.Leg.LEFT)
    BinaryTree.place_distributor(child, grandchild, leg=BinaryTreeEdge.Leg.LEFT)

    tree = get_downline_tree(root)

    assert len(tree.children) == 1
    child_node = tree.children[0]
    assert child_node.full_name == "Child"
    assert len(child_node.children) == 1
    assert child_node.children[0].full_name == "Grandchild"


@pytest.mark.django_db
def test_node_pv_reflects_that_persons_own_left_and_right_leg_totals():
    root = _make_distributor("root4", full_name="Root Four", ir_id="IR00008")
    child = _make_distributor("child4", full_name="Child Four", ir_id="IR00009")
    BinaryTree.place_distributor(root, child, leg=BinaryTreeEdge.Leg.LEFT)
    grandchild_left = _make_distributor("gcl4", full_name="GC Left", ir_id="IR00010")
    grandchild_right = _make_distributor("gcr4", full_name="GC Right", ir_id="IR00011")
    BinaryTree.place_distributor(child, grandchild_left, leg=BinaryTreeEdge.Leg.LEFT)
    BinaryTree.place_distributor(child, grandchild_right, leg=BinaryTreeEdge.Leg.RIGHT)

    # Credit PV up the ancestor chain from a purchase by grandchild_left,
    # so `child`'s own left_leg_pv becomes non-zero.
    record_purchase_pv(grandchild_left, pv_amount=50)

    tree = get_downline_tree(root)
    child_node = tree.children[0]

    assert child_node.pv == 50


@pytest.mark.django_db
def test_query_count_does_not_grow_with_downline_size():
    """Task 21a's core acceptance criterion: this must stay O(1) queries
    regardless of downline size, never a recursive per-node walk (SPEC.md
    Scale Architecture)."""
    root = _make_distributor("root5", full_name="Root Five", ir_id="IR00012")
    small_child = _make_distributor("small5", full_name="Small", ir_id="IR00013")
    BinaryTree.place_distributor(root, small_child, leg=BinaryTreeEdge.Leg.LEFT)

    with CaptureQueriesContext(connection) as small_ctx:
        get_downline_tree(root)
    small_query_count = len(small_ctx.captured_queries)

    root2 = _make_distributor("root6", full_name="Root Six", ir_id="IR00014")
    current_leg = BinaryTreeEdge.Leg.LEFT
    parent = root2
    for i in range(20):
        node = _make_distributor(f"chain{i}", full_name=f"Chain {i}", ir_id=f"IR{20000+i}")
        BinaryTree.place_distributor(parent, node, leg=current_leg)
        parent = node

    with CaptureQueriesContext(connection) as large_ctx:
        get_downline_tree(root2)
    large_query_count = len(large_ctx.captured_queries)

    assert large_query_count == small_query_count
