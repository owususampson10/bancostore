from itertools import count

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from apps.binary_tree.models import BinaryTreeEdge
from apps.binary_tree.services import AncestorPvAggregate, get_ancestor_pv_aggregates
from apps.distributors.models import Distributor
from apps.pv_ledger.models import PvLedger

User = get_user_model()

_phone_seq = count(1)


def _make_distributor(name):
    user = User.objects.create_user(username=name, password="testpass123")
    phone = f"+233400{next(_phone_seq):06d}"
    return Distributor.objects.create(user=user, phone_number=phone)


@pytest.mark.django_db
def test_returns_the_correct_pv_totals_for_a_small_hand_built_tree():
    sponsor = _make_distributor("sponsor_q1")
    parent = _make_distributor("parent_q1")
    leaf = _make_distributor("leaf_q1")

    PvLedger.objects.create(distributor=sponsor, left_leg_pv=10, right_leg_pv=20)
    PvLedger.objects.create(distributor=parent, left_leg_pv=5, right_leg_pv=0)
    PvLedger.objects.create(distributor=leaf, left_leg_pv=0, right_leg_pv=0)

    BinaryTreeEdge.objects.create(
        ancestor=sponsor, descendant=parent, depth=1, leg=BinaryTreeEdge.Leg.LEFT
    )
    BinaryTreeEdge.objects.create(
        ancestor=parent, descendant=leaf, depth=1, leg=BinaryTreeEdge.Leg.RIGHT
    )
    BinaryTreeEdge.objects.create(
        ancestor=sponsor, descendant=leaf, depth=2, leg=BinaryTreeEdge.Leg.LEFT
    )

    aggregates = get_ancestor_pv_aggregates(leaf)

    assert aggregates == [
        AncestorPvAggregate(
            ancestor_id=parent.pk,
            depth=1,
            leg=BinaryTreeEdge.Leg.RIGHT,
            left_leg_pv=5,
            right_leg_pv=0,
        ),
        AncestorPvAggregate(
            ancestor_id=sponsor.pk,
            depth=2,
            leg=BinaryTreeEdge.Leg.LEFT,
            left_leg_pv=10,
            right_leg_pv=20,
        ),
    ]


@pytest.mark.django_db
def test_returns_no_rows_for_a_distributor_with_no_ancestors():
    root = _make_distributor("root_q2")
    PvLedger.objects.create(distributor=root)

    assert get_ancestor_pv_aggregates(root) == []


@pytest.mark.django_db
def test_missing_pv_ledger_defaults_to_zero_instead_of_crashing_or_dropping_the_row():
    sponsor = _make_distributor("sponsor_q3")
    leaf = _make_distributor("leaf_q3")
    # Deliberately no PvLedger row for `sponsor` -- shouldn't happen via
    # BinaryTree.place_distributor, but the query must stay correct even if
    # a row is ever missing (e.g. a Distributor created outside that path).
    BinaryTreeEdge.objects.create(
        ancestor=sponsor, descendant=leaf, depth=1, leg=BinaryTreeEdge.Leg.LEFT
    )

    aggregates = get_ancestor_pv_aggregates(leaf)

    assert aggregates == [
        AncestorPvAggregate(
            ancestor_id=sponsor.pk,
            depth=1,
            leg=BinaryTreeEdge.Leg.LEFT,
            left_leg_pv=0,
            right_leg_pv=0,
        )
    ]


def _build_complete_binary_tree(total_nodes):
    """Bulk-build a complete binary tree's closure table directly (bypassing
    BinaryTree.place_distributor, which is already covered by Task 9b's own
    tests) so a 10k+ node scale test runs in seconds, not tens of minutes.
    Nodes are 1-indexed in heap layout: node i's parent is i // 2, and node i
    is its parent's LEFT child if even, RIGHT child if odd."""
    placeholder_password = make_password(None)
    User.objects.bulk_create(
        [
            User(username=f"scale_{i}", password=placeholder_password)
            for i in range(1, total_nodes + 1)
        ]
    )
    # bulk_create's returned PKs aren't guaranteed on every backend (e.g.
    # MySQL) -- refetch in a deterministic, creation-matching order instead.
    user_ids = list(
        User.objects.filter(username__startswith="scale_")
        .order_by("id")
        .values_list("id", flat=True)
    )

    Distributor.objects.bulk_create(
        [
            Distributor(user_id=user_ids[i - 1], phone_number=f"+233500{i:06d}")
            for i in range(1, total_nodes + 1)
        ]
    )
    distributor_ids = list(
        Distributor.objects.filter(phone_number__startswith="+233500")
        .order_by("phone_number")
        .values_list("id", flat=True)
    )
    # node index i (1-based) -> distributor_ids[i - 1]
    node_id = {i: distributor_ids[i - 1] for i in range(1, total_nodes + 1)}

    ancestor_chains = {}  # node index -> [(ancestor_index, depth, leg), ...]
    edges = []
    for i in range(2, total_nodes + 1):
        parent = i // 2
        local_leg = BinaryTreeEdge.Leg.LEFT if i % 2 == 0 else BinaryTreeEdge.Leg.RIGHT
        chain = [(parent, 1, local_leg)]
        chain.extend(
            (anc, depth + 1, leg) for anc, depth, leg in ancestor_chains.get(parent, [])
        )
        ancestor_chains[i] = chain
        edges.extend(
            BinaryTreeEdge(
                ancestor_id=node_id[anc],
                descendant_id=node_id[i],
                depth=depth,
                leg=leg,
            )
            for anc, depth, leg in chain
        )

    BinaryTreeEdge.objects.bulk_create(edges, batch_size=5000)
    PvLedger.objects.bulk_create(
        [PvLedger(distributor_id=did) for did in distributor_ids], batch_size=5000
    )
    return node_id


@pytest.mark.django_db
def test_ancestor_query_is_a_single_query_regardless_of_tree_size_or_depth():
    total_nodes = 16383  # complete binary tree of depth 13 (2^14 - 1), well over 10k
    node_id = _build_complete_binary_tree(total_nodes)
    deepest_node = Distributor.objects.get(pk=node_id[total_nodes])

    with CaptureQueriesContext(connection) as ctx:
        aggregates = get_ancestor_pv_aggregates(deepest_node)

    # django-silk instruments every query and, depending on test-run
    # ordering, can append its own "EXPLAIN QUERY PLAN ..." diagnostic call
    # re-running the same SELECT -- dev-tooling overhead, not application
    # work, and (being a fixed re-run of the one real query) it doesn't grow
    # with tree depth either. Filter it out before counting the real query.
    real_queries = [
        q for q in ctx.captured_queries if not q["sql"].startswith("EXPLAIN")
    ]
    assert len(real_queries) == 1

    # depth of the last node in a complete tree of this size is exactly 13
    assert len(aggregates) == 13
    assert [row.depth for row in aggregates] == list(range(1, 14))
