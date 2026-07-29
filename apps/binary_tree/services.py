from collections import deque
from typing import NamedTuple

from django.db import transaction

from apps.distributors.models import Distributor
from apps.notifications.models import Notification
from apps.notifications.services import send_notification
from apps.pv_ledger.models import PvLedger
from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

from .models import BinaryTreeEdge


class AlreadyPlacedError(Exception):
    pass


class BinaryTree:
    """Placement + spillover service (Task 9b).

    Algorithm confirmed with the user 2026-07-13 (see tasks/todo.md Task 9):
    the sponsor picks a leg explicitly at registration, or -- if they don't --
    auto-balance picks whichever of the sponsor's two legs currently has less
    PV (ties default to LEFT). Spillover then never crosses to the other leg:
    it fills the shallowest open slot first, left before right at each level.

    place_distributor raises AlreadyPlacedError if new_distributor already
    has a direct parent -- callers should treat that as a caller bug (the
    same distributor placed twice), not something to retry.
    """

    @staticmethod
    def place_distributor(sponsor, new_distributor, leg=None):
        def _attempt():
            with transaction.atomic():
                # Lock sponsor and new_distributor in a fixed, PK-ascending
                # order -- not call-argument order -- so two concurrent
                # placements can never lock the same two rows in opposite
                # order and deadlock. This serializes placements that share
                # a sponsor or a new_distributor, but not a placement whose
                # explicit sponsor is a node a *different* in-flight
                # spillover is about to land on -- that intermediate node
                # is never locked here. Closing that gap needs whole-path
                # locking, which isn't done; revisit once Task 10 exercises
                # this under real concurrent registration load.
                pks_to_lock = sorted(
                    {new_distributor.pk} | ({sponsor.pk} if sponsor else set())
                )
                for pk in pks_to_lock:
                    select_for_update_nowait_if_supported(
                        Distributor.objects.filter(pk=pk)
                    ).get()

                # A node has exactly one direct parent. This can't be a DB
                # constraint on MySQL (see the comment in models.py), so
                # it's enforced here, inside the same lock: without it, two
                # concurrent placements for the same new_distributor under
                # different sponsors could both succeed, silently giving
                # one node two parents.
                if BinaryTreeEdge.objects.filter(
                    descendant=new_distributor, depth=1
                ).exists():
                    raise AlreadyPlacedError(
                        f"{new_distributor} is already placed in the binary tree."
                    )

                # PvLedger creation must live inside the same retried
                # transaction as everything else below -- SQLite's single
                # whole-database write lock means an unprotected write here
                # can itself raise "database is locked" under concurrent
                # threads, even though it touches an unrelated table/row.
                # Every distributor gets one unconditionally -- Task 9c's
                # ancestor-aggregate query depends on this, and neither "no
                # sponsor" (the tree root) nor "sponsor picked an explicit
                # leg" (skipping the old auto-balance-only creation path)
                # should be exceptions.
                PvLedger.objects.get_or_create(distributor=new_distributor)

                if sponsor is None:
                    # The very first distributor in the system has no
                    # sponsor and no place in anyone else's tree.
                    return

                sponsor_ledger, _ = PvLedger.objects.get_or_create(distributor=sponsor)
                chosen_leg = leg or BinaryTree._weaker_leg(sponsor_ledger)
                parent, local_leg = BinaryTree._find_open_slot(sponsor, chosen_leg)
                BinaryTree._attach(parent, local_leg, new_distributor)

                # Task 21d-ii: Section 6.6's "someone new joined under
                # them" -- notifies the SPONSOR (who directly referred
                # this person), not `parent` (the tree-placement parent,
                # which spillover can make a different, deeper node with
                # no real relationship to the new distributor).
                new_distributor_name = new_distributor.full_name or str(
                    new_distributor.phone_number
                )
                send_notification(
                    sponsor,
                    Notification.EventType.DOWNLINE_JOINED,
                    f"{new_distributor_name} just joined your team!",
                )

        retry_on_lock_contention(_attempt)

    @staticmethod
    def _weaker_leg(ledger):
        if ledger.right_leg_pv < ledger.left_leg_pv:
            return BinaryTreeEdge.Leg.RIGHT
        return BinaryTreeEdge.Leg.LEFT

    @staticmethod
    def _direct_child(node, leg_value):
        edge = (
            BinaryTreeEdge.objects.filter(ancestor=node, depth=1, leg=leg_value)
            .select_related("descendant")
            .first()
        )
        return edge.descendant if edge else None

    @staticmethod
    def _find_open_slot(sponsor, chosen_leg):
        """Breadth-first search within `chosen_leg`'s subtree: shallowest
        open slot first, left before right. Never looks at the other leg."""
        child = BinaryTree._direct_child(sponsor, chosen_leg)
        if child is None:
            return sponsor, chosen_leg

        queue = [child]
        while queue:
            node = queue.pop(0)
            left_child = BinaryTree._direct_child(node, BinaryTreeEdge.Leg.LEFT)
            if left_child is None:
                return node, BinaryTreeEdge.Leg.LEFT
            right_child = BinaryTree._direct_child(node, BinaryTreeEdge.Leg.RIGHT)
            if right_child is None:
                return node, BinaryTreeEdge.Leg.RIGHT
            queue.append(left_child)
            queue.append(right_child)

        raise AssertionError(
            "unreachable: a binary tree always has an open slot somewhere"
        )

    @staticmethod
    def _attach(parent, local_leg, new_distributor):
        BinaryTreeEdge.objects.create(
            ancestor=parent, descendant=new_distributor, depth=1, leg=local_leg
        )
        ancestor_edges = BinaryTreeEdge.objects.filter(descendant=parent)
        BinaryTreeEdge.objects.bulk_create(
            [
                BinaryTreeEdge(
                    ancestor_id=edge.ancestor_id,
                    descendant=new_distributor,
                    depth=edge.depth + 1,
                    leg=edge.leg,
                )
                for edge in ancestor_edges
            ]
        )


class AncestorPvAggregate(NamedTuple):
    ancestor_id: int
    depth: int
    leg: str
    left_leg_pv: int
    right_leg_pv: int


def get_ancestor_pv_aggregates(distributor):
    """Task 9c: every ancestor of `distributor` with their current leg-PV
    totals, ordered shallowest-first, in exactly one query -- never a
    recursive tree walk. This is what the (future) 10-minute binary bonus
    task reads; its cost must not grow with tree depth or width.

    A `.values()` projection is used deliberately instead of attribute
    access (`edge.ancestor.pv_ledger`): the latter raises
    PvLedger.DoesNotExist for any ancestor missing a ledger row, which would
    crash this for every distributor sharing that ancestor. `.values()`
    performs the same LEFT OUTER JOIN in SQL and returns NULL columns
    instead, which the `or 0` below turns into a safe default.
    """
    rows = (
        BinaryTreeEdge.objects.filter(descendant=distributor)
        .order_by("depth")
        .values(
            "ancestor_id",
            "depth",
            "leg",
            "ancestor__pv_ledger__left_leg_pv",
            "ancestor__pv_ledger__right_leg_pv",
        )
    )
    return [
        AncestorPvAggregate(
            ancestor_id=row["ancestor_id"],
            depth=row["depth"],
            leg=row["leg"],
            left_leg_pv=row["ancestor__pv_ledger__left_leg_pv"] or 0,
            right_leg_pv=row["ancestor__pv_ledger__right_leg_pv"] or 0,
        )
        for row in rows
    ]


class DownlineNode(NamedTuple):
    distributor_id: int
    full_name: str
    ir_id: str
    rank: str
    pv: int
    leg: str
    children: list


def get_downline_tree(distributor):
    """Task 21a: `distributor`'s entire downline as a nested tree (name, IR
    ID, rank, PV per node), for the dashboard's visual binary tree view.

    Exactly three queries regardless of downline size or depth -- never a
    recursive walk (SPEC.md Scale Architecture):

    1. `distributor`'s own left/right leg PV (it has no incoming
       BinaryTreeEdge as its own ancestor, so it can't be picked up by the
       descendant query below).
    2. Every descendant with the data a node needs to render, in one
       `.values()` LEFT OUTER JOIN (same "row['x__pv_ledger__y'] or 0"
       pattern as get_ancestor_pv_aggregates above, for the same reason:
       attribute access would raise PvLedger.DoesNotExist for any
       descendant who hasn't purchased anything yet).
    3. Every *direct* (depth=1) parent-child edge among `distributor`
       itself plus all of its descendants -- this reconstructs the whole
       subtree's adjacency in one shot, since a depth=1 edge is exactly a
       direct parent-child pair, at any level of the tree, not just
       `distributor`'s own direct children.

    The tree itself is then assembled in memory (O(n) in Python, not the
    database) from those three already-fetched result sets.
    """
    descendant_rows = list(
        BinaryTreeEdge.objects.filter(ancestor=distributor).values(
            "descendant_id",
            "descendant__full_name",
            "descendant__ir_id",
            "descendant__rank",
            "descendant__pv_ledger__left_leg_pv",
            "descendant__pv_ledger__right_leg_pv",
        )
    )

    root_pv = (
        Distributor.objects.filter(pk=distributor.pk)
        .values("pv_ledger__left_leg_pv", "pv_ledger__right_leg_pv")
        .first()
    )

    node_data = {
        distributor.pk: {
            "full_name": distributor.full_name,
            "ir_id": distributor.ir_id or "",
            "rank": distributor.rank,
            "pv": (root_pv["pv_ledger__left_leg_pv"] or 0)
            + (root_pv["pv_ledger__right_leg_pv"] or 0),
        }
    }
    for row in descendant_rows:
        node_data[row["descendant_id"]] = {
            "full_name": row["descendant__full_name"],
            "ir_id": row["descendant__ir_id"] or "",
            "rank": row["descendant__rank"],
            "pv": (row["descendant__pv_ledger__left_leg_pv"] or 0)
            + (row["descendant__pv_ledger__right_leg_pv"] or 0),
        }

    descendant_ids = list(node_data.keys())
    direct_edges = BinaryTreeEdge.objects.filter(
        ancestor_id__in=descendant_ids, depth=1
    ).values("ancestor_id", "descendant_id", "leg")

    children_by_parent: dict = {}
    for edge in direct_edges:
        children_by_parent.setdefault(edge["ancestor_id"], []).append(edge)

    # Iterative, not recursive (CodeRabbit finding on PR #44): a pathologically
    # deep single-line downline (every distributor sponsoring exactly one
    # next distributor, never spilling over) could in principle exceed
    # Python's default recursion limit. A BFS visit order guarantees every
    # node appears before its own children (they're exactly one level
    # deeper), so building DownlineNode tuples in *reverse* visit order
    # guarantees each node's children are already built by the time it's
    # its own turn -- without ever recursing.
    visit_order = []
    queue = deque([(distributor.pk, "")])
    while queue:
        node_id, leg = queue.popleft()
        visit_order.append((node_id, leg))
        for edge in sorted(children_by_parent.get(node_id, []), key=lambda e: e["leg"]):
            queue.append((edge["descendant_id"], edge["leg"]))

    built: dict = {}
    for node_id, leg in reversed(visit_order):
        data = node_data[node_id]
        child_edges = sorted(
            children_by_parent.get(node_id, []), key=lambda e: e["leg"]
        )
        built[node_id] = DownlineNode(
            distributor_id=node_id,
            full_name=data["full_name"],
            ir_id=data["ir_id"],
            rank=data["rank"],
            pv=data["pv"],
            leg=leg,
            children=[built[edge["descendant_id"]] for edge in child_edges],
        )

    return built[distributor.pk]
