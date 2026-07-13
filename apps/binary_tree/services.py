from django.db import transaction

from apps.distributors.models import Distributor
from apps.pv_ledger.models import PvLedger
from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

from .models import BinaryTreeEdge


class BinaryTree:
    """Placement + spillover service (Task 9b).

    Algorithm confirmed with the user 2026-07-13 (see tasks/todo.md Task 9):
    the sponsor picks a leg explicitly at registration, or -- if they don't --
    auto-balance picks whichever of the sponsor's two legs currently has less
    PV (ties default to LEFT). Spillover then never crosses to the other leg:
    it fills the shallowest open slot first, left before right at each level.
    """

    @staticmethod
    def place_distributor(sponsor, new_distributor, leg=None):
        if sponsor is None:
            # The very first distributor in the system has no sponsor and no
            # place in anyone else's tree.
            return

        def _attempt():
            with transaction.atomic():
                # PvLedger creation must live inside the same retried
                # transaction as everything else below -- SQLite's single
                # whole-database write lock means an unprotected write here
                # can itself raise "database is locked" under concurrent
                # threads, even though it touches an unrelated table/row.
                PvLedger.objects.get_or_create(distributor=new_distributor)
                # Lock the sponsor row so two concurrent placements under the
                # same sponsor serialize instead of racing: without this,
                # _find_open_slot's read and _attach's write are a classic
                # check-then-act -- both calls could see the same "open" slot
                # and both attach there, giving one node two direct children
                # on the same leg. This doesn't protect a placement that
                # targets, as its explicit sponsor, a node another in-flight
                # spillover is about to land on -- that needs whole-path
                # locking, which isn't done here; see the review note for
                # Task 9b.
                select_for_update_nowait_if_supported(
                    Distributor.objects.filter(pk=sponsor.pk)
                ).get()
                chosen_leg = leg or BinaryTree._weaker_leg(sponsor)
                parent, local_leg = BinaryTree._find_open_slot(sponsor, chosen_leg)
                BinaryTree._attach(parent, local_leg, new_distributor)

        retry_on_lock_contention(_attempt)

    @staticmethod
    def _weaker_leg(sponsor):
        ledger, _ = PvLedger.objects.get_or_create(distributor=sponsor)
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
