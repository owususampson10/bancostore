import logging
import time

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from constance import config

from apps.binary_tree.models import BinaryTreeEdge
from bancostore.concurrency import RETRY_BACKOFF_SECONDS

from .models import MonthlyPersonalPv, PvDailyBucket, PvLedger

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

    today = timezone.now().date()
    for field, leg, ancestor_ids in (
        ("left_leg_pv", BinaryTreeEdge.Leg.LEFT, left_ids),
        ("right_leg_pv", BinaryTreeEdge.Leg.RIGHT, right_ids),
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
        _credit_daily_buckets(ancestor_ids, leg, pv_amount, today)


def _credit_daily_buckets(ancestor_ids, leg, pv_amount, today, max_retries=3):
    """Credits `pv_amount` to today's PvDailyBucket row for every id in
    `ancestor_ids` on `leg` -- creating the row on first credit of the
    day, incrementing it otherwise. Two bulk statements per attempt (a
    bulk UPDATE, then a bulk_create for whoever didn't already have a row
    today), matching record_purchase_pv's own PvLedger pattern -- never
    one query per ancestor, however deep the tree.

    `ancestor_ids` is expected to already be deduplicated and to exclude
    the purchasing distributor -- it's the exact left_ids/right_ids list
    record_purchase_pv already built (from BinaryTreeEdge, which has a
    DB constraint banning ancestor == descendant and a unique constraint
    on (ancestor, descendant), so both properties already hold), reused
    here rather than recomputed.

    Unlike PvLedger rows (which always pre-exist once a distributor is
    placed), a PvDailyBucket row doesn't exist until the first credit of
    a given day, so two concurrent first-purchases-of-the-day for the
    same ancestor+leg can race two INSERTs against the unique
    constraint. That's a different hazard than the row-lock contention
    bancostore.concurrency.retry_on_lock_contention targets (that helper
    retries an OperationalError from a blocked/timed-out lock on an
    EXISTING row); this is an IntegrityError from two transactions both
    trying to CREATE the same new row, so it needs this function's own
    bounded retry in addition to, not instead of, that helper. Any
    genuine lock-contention OperationalError raised along the way (e.g.
    a MySQL deadlock during the bulk_create) is NOT caught here -- it
    propagates to the outer retry_on_lock_contention wrapper that
    already surrounds the whole purchase attempt in
    consume_paid_starter_pack, exactly like this same function's bulk
    UPDATE call above already relies on for its own OperationalErrors.
    Catching it here too would nest a second retry loop on the same
    error class the outer wrapper already retries -- the exact
    amplification bug this project already found and fixed once in
    apps/wallet/services.py::credit().

    Raises RuntimeError (does not silently drop the credit) if retries
    are exhausted -- this aborts the caller's whole purchase transaction
    rather than let a purchase record its PvLedger total but silently
    skip its daily bucket, since future binary-bonus PV calculations
    read only the bucket, never re-derive it from PvLedger.

    Assumes `ancestor_ids` stays well under SQLite's ~999-bound-variable
    `IN (...)` limit -- true at today's tree depths (~20 ancestors/leg,
    verified against a 16k+-node tree) with ~50x headroom, but revisit
    with chunking if tree depth assumptions ever change materially."""
    remaining_ids = list(ancestor_ids)
    for attempt in range(1, max_retries + 1):
        if not remaining_ids:
            return

        try:
            with transaction.atomic():
                PvDailyBucket.objects.filter(
                    distributor_id__in=remaining_ids, leg=leg, date=today
                ).update(pv=F("pv") + pv_amount)

                existing_ids = set(
                    PvDailyBucket.objects.filter(
                        distributor_id__in=remaining_ids, leg=leg, date=today
                    ).values_list("distributor_id", flat=True)
                )
                missing_ids = [d for d in remaining_ids if d not in existing_ids]
                if missing_ids:
                    PvDailyBucket.objects.bulk_create(
                        [
                            PvDailyBucket(
                                distributor_id=d, leg=leg, date=today, pv=pv_amount
                            )
                            for d in missing_ids
                        ]
                    )
            return
        except IntegrityError:
            # A concurrent transaction created one of these rows between
            # our existence check and our own insert. The whole attempt
            # rolled back together, INCLUDING the bulk UPDATE above --
            # so anyone in `remaining_ids` who already had a row (and
            # got incremented by that update) needs to be retried too,
            # not just the ones that were missing. Do NOT narrow
            # remaining_ids to missing_ids here: that was correct back
            # when only the create step was wrapped in a savepoint (the
            # update had already durably committed by then), but once
            # the whole attempt became one atomic block, narrowing
            # silently drops the rolled-back update for anyone not in
            # missing_ids -- found via a real threaded test that lost
            # exactly this PV with zero exceptions raised.
            logger.warning(
                "_credit_daily_buckets: attempt=%s hit a concurrent insert "
                "for leg=%s date=%s -- retrying ancestor_ids=%s.",
                attempt,
                leg,
                today,
                remaining_ids,
            )
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

    logger.critical(
        "_credit_daily_buckets: exhausted %s retries crediting leg=%s pv=%s "
        "date=%s for ancestor_ids=%s -- aborting the whole purchase "
        "transaction. Needs manual investigation.",
        max_retries,
        leg,
        pv_amount,
        today,
        remaining_ids,
    )
    raise RuntimeError(
        f"_credit_daily_buckets: exhausted {max_retries} retries crediting "
        f"leg={leg} pv={pv_amount} date={today} for ancestor_ids={remaining_ids} "
        "-- needs manual investigation."
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
