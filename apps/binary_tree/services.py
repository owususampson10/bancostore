from typing import NamedTuple

from django.db import transaction

from apps.distributors.models import Distributor
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
