from itertools import count

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

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


@pytest.mark.django_db
def test_query_count_does_not_grow_with_ancestor_depth():
    """Regression guard for the N+1 this function was rewritten to avoid
    (one query per ancestor). Mirrors
    tests/unit/binary_tree/test_ancestor_query.py's
    test_ancestor_query_is_a_single_query_regardless_of_tree_size_or_depth
    -- this project's established way of proving a scale-sensitive path
    stays flat as the tree grows, per SPEC.md's Scale Architecture."""
    chain_length = 50
    sponsor = _make_distributor()
    for i in range(chain_length):
        newcomer = _make_distributor()
        leg = BinaryTreeEdge.Leg.LEFT if i % 2 == 0 else BinaryTreeEdge.Leg.RIGHT
        BinaryTree.place_distributor(sponsor, newcomer, leg=leg)
        sponsor = newcomer
    deepest = sponsor

    with CaptureQueriesContext(connection) as ctx:
        record_purchase_pv(deepest, 500)

    # Same EXPLAIN-filtering rationale as test_ancestor_query.py: django-silk
    # can append its own diagnostic re-run of a query, which is fixed
    # overhead, not application work, and doesn't grow with depth either.
    real_queries = [
        q for q in ctx.captured_queries if not q["sql"].startswith("EXPLAIN")
    ]
    # 1 query to fetch this distributor's ancestor edges, plus, per leg
    # that actually has ancestors on it (at most 2): 1 bulk UPDATE for
    # PvLedger, then _credit_daily_buckets's own bulk UPDATE + existence
    # SELECT + a savepoint-wrapped bulk_create (SAVEPOINT/INSERT/RELEASE)
    # for whoever has no bucket yet today -- a fixed handful of
    # statements per leg, never one query per ancestor regardless of how
    # deep the chain is.
    assert len(real_queries) <= 15
