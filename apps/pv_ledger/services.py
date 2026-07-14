import logging

from django.db.models import F
from django.utils import timezone

from constance import config

from apps.binary_tree.models import BinaryTreeEdge

from .models import MonthlyPersonalPv, PvLedger

logger = logging.getLogger(__name__)


def record_purchase_pv(distributor, pv_amount):
    """Task 10d: credits `pv_amount` to the correct leg of every ancestor
    of `distributor`, event-driven at write time (SPEC.md Scale
    Architecture) -- never a recursive tree walk or a full recompute.

    `distributor` must already be placed in the binary tree (see
    apps.binary_tree.services.BinaryTree.place_distributor) before this is
    called -- it reads existing BinaryTreeEdge rows, it does not create
    placement.

    Ancestors are grouped by leg and credited with two bulk `UPDATE ...
    SET left_leg_pv = left_leg_pv + %s WHERE distributor_id IN (...)`
    statements (via `F()`), not one query per ancestor -- this is called
    on every purchase confirmation, and a per-ancestor loop would turn
    into an unbounded-with-tree-depth query count at the exact scale
    SPEC.md's Scale Architecture is designed to avoid. The `F()` update
    itself is race-free without select_for_update: the increment happens
    entirely inside the database in one statement.
    """
    if not pv_amount:
        return

    edges = list(BinaryTreeEdge.objects.filter(descendant=distributor))
    if not edges:
        return

    left_ids = [
        edge.ancestor_id for edge in edges if edge.leg == BinaryTreeEdge.Leg.LEFT
    ]
    right_ids = [
        edge.ancestor_id for edge in edges if edge.leg == BinaryTreeEdge.Leg.RIGHT
    ]

    for field, ancestor_ids in (
        ("left_leg_pv", left_ids),
        ("right_leg_pv", right_ids),
    ):
        if not ancestor_ids:
            continue
        updated = PvLedger.objects.filter(distributor_id__in=ancestor_ids).update(
            **{field: F(field) + pv_amount}
        )
        if updated != len(ancestor_ids):
            existing = PvLedger.objects.filter(
                distributor_id__in=ancestor_ids
            ).values_list("distributor_id", flat=True)
            missing = sorted(set(ancestor_ids) - set(existing))
            logger.error(
                "record_purchase_pv: missing PvLedger row(s) for "
                "ancestor(s)=%s while crediting distributor=%s -- %s PV "
                "was NOT recorded for these ancestors. Needs manual "
                "investigation.",
                missing,
                distributor.pk,
                pv_amount,
            )


def record_personal_pv(distributor, pv_amount) -> None:
    """Credits `pv_amount` to `distributor`'s OWN monthly personal PV
    total -- distinct from record_purchase_pv, which credits PV to the
    purchasing distributor's ANCESTORS' legs, never their own record.
    Needed for the Binary/Matching Bonus "minimum personal PV this month"
    eligibility rule.

    Lazily creates the current month's row (mirrors PvLedger's own
    get_or_create convention) and does not retry on lock contention
    itself -- same reasoning as apps/wallet/services.py::credit(): this
    is called from within consume_paid_starter_pack's already-retried
    locked block, and an internal retry would nest and amplify under
    contention rather than just retrying the credit."""
    if not pv_amount:
        return

    period = timezone.now().date().replace(day=1)
    row, _ = MonthlyPersonalPv.objects.get_or_create(
        distributor=distributor, period=period
    )
    MonthlyPersonalPv.objects.filter(pk=row.pk).update(pv=F("pv") + pv_amount)


def is_eligible_for_binary_bonus(distributor) -> bool:
    """Has `distributor` personally generated at least
    config.MIN_MONTHLY_PERSONAL_PV in the current calendar month? Reads
    the pre-aggregated MonthlyPersonalPv row only -- never recomputes
    from raw purchase history."""
    period = timezone.now().date().replace(day=1)
    try:
        row = MonthlyPersonalPv.objects.get(distributor=distributor, period=period)
    except MonthlyPersonalPv.DoesNotExist:
        return False
    return row.pv >= config.MIN_MONTHLY_PERSONAL_PV
