import logging
import time

from django.db import IntegrityError, transaction
from django.db.models import Case, F, PositiveIntegerField, Sum, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from constance import config

from apps.binary_tree.models import BinaryTreeEdge
from apps.distributors.models import Distributor
from bancostore.concurrency import (
    RETRY_BACKOFF_SECONDS,
    select_for_update_nowait_if_supported,
)

from .models import MonthlyPersonalPv, PvDailyBucket, PvLedger

logger = logging.getLogger(__name__)


def record_purchase_pv(distributor, pv_amount, today=None):
    """Task 10d: credits `pv_amount` to the correct leg of every ancestor
    of `distributor`, event-driven at write time (SPEC.md Scale
    Architecture) -- never a recursive tree walk or a full recompute.

    `today` (Task 18b) lets a caller pin the exact date this credit lands
    in `PvDailyBucket`, instead of implicitly using whatever
    `timezone.now().date()` happens to be at call time. `confirm_order_payment`
    passes this explicitly so a later cancel/refund reversal can target the
    exact bucket the original credit landed in, with no drift between two
    independent `timezone.now()` calls. Defaults to `timezone.now().date()`
    when omitted, matching every existing caller's behavior unchanged.

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

    today = today or timezone.now().date()
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
    day, incrementing it otherwise. Up to three statements per attempt (a
    locking existence-check read, then a bulk UPDATE for whichever ids
    that read found already have a row today, and/or a bulk_create for
    whichever it found missing -- both can run in the same attempt for a
    mixed batch, they aren't mutually exclusive), matching
    record_purchase_pv's own PvLedger pattern -- never one query per
    ancestor, however deep the tree.

    `ancestor_ids` is expected to already be deduplicated and to exclude
    the purchasing distributor -- it's the exact left_ids/right_ids list
    record_purchase_pv already built (from BinaryTreeEdge, which has a
    DB constraint banning ancestor == descendant and a unique constraint
    on (ancestor, descendant), so both properties already hold), reused
    here rather than recomputed.

    **2026-07-22: fixed a real silent-credit-loss bug caught by real MySQL
    in CI** (a 10-way concurrent-thread test reported zero exceptions but
    landed 90 PV instead of 100 -- see tests/unit/pv_ledger/
    test_daily_buckets.py::
    test_concurrent_first_of_day_credits_to_the_same_ancestor_never_lose_pv).
    The PREVIOUS version ran a blind bulk UPDATE first (matching 0 rows
    when nothing exists yet for today), THEN a separate plain SELECT to
    decide what's still missing. Confirmed against MySQL 8.0's own InnoDB
    consistent-read docs: under REPEATABLE READ, an UPDATE does NOT
    establish the transaction's snapshot for later plain SELECTs -- "the
    snapshot of the database state applies to SELECT statements... not
    necessarily to DML statements" -- so that later SELECT establishes its
    OWN fresh snapshot, independent of what the UPDATE saw. If another
    transaction committed an INSERT for this exact row in the gap between
    this transaction's UPDATE and its own later SELECT, that SELECT would
    see the row as "already existing" and skip both the UPDATE (already
    ran, before the row existed, so never touched it) and the bulk_create
    (skipped, since the row is no longer "missing") -- silently dropping
    this attempt's whole pv_amount with no exception anywhere. Only
    possible under MySQL's per-statement snapshot semantics; SQLite
    serializes all writes at the whole-database level, so this specific
    interleaving can't occur there, which is exactly why it only ever
    surfaced in CI.

    Fixed by using ONE locking read (select_for_update_nowait_if_
    supported, this project's own established helper) to determine BOTH
    which ids already have a row (-> UPDATE) and which don't (-> INSERT),
    instead of independently re-deriving that answer via a second, later,
    freshly-snapshotted read. There's no longer a window where the
    UPDATE-set and the INSERT-set can disagree with each other, because
    they're now derived from the exact same read. (This is not primarily
    about lock acquisition on the missing rows -- confirmed against
    MySQL's own locking-reads docs that a unique-index equality lookup
    matching zero rows takes no gap lock at all, so two transactions can
    still race to be the first to create the row; that race was already
    handled correctly by the IntegrityError retry below, and still is.)

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
    from the NOWAIT existence-check read itself, or a MySQL deadlock
    during the bulk_create) is NOT caught here -- it propagates to the
    outer retry_on_lock_contention wrapper that already surrounds the
    whole purchase attempt in consume_paid_starter_pack. Catching it here
    too would nest a second retry loop on the same error class the outer
    wrapper already retries -- the exact amplification bug this project
    already found and fixed once in apps/wallet/services.py::credit().

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
            # Nested inside the caller's already-open atomic() block, this
            # creates a real SAVEPOINT (not a no-op) and rolls back only
            # this attempt's work on exception, leaving the caller's
            # earlier work in the same transaction intact and still
            # committable -- documented Django behavior, not assumed:
            # "creates a savepoint when entering an inner atomic block;
            # releases or rolls back to the savepoint when exiting an
            # inner block." https://docs.djangoproject.com/en/5.0/topics/db/transactions/#savepoints
            with transaction.atomic():
                existing_ids = set(
                    select_for_update_nowait_if_supported(
                        PvDailyBucket.objects.filter(
                            distributor_id__in=remaining_ids, leg=leg, date=today
                        )
                    ).values_list("distributor_id", flat=True)
                )
                missing_ids = [d for d in remaining_ids if d not in existing_ids]

                if existing_ids:
                    PvDailyBucket.objects.filter(
                        distributor_id__in=existing_ids, leg=leg, date=today
                    ).update(pv=F("pv") + pv_amount)

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
            # Two transactions' existence-check reads both matched zero
            # rows for the same id (expected and unavoidable: a unique-
            # index lookup that matches nothing takes no lock in InnoDB,
            # confirmed against MySQL's own locking-reads docs -- so this
            # isn't preventable by the select_for_update above, only
            # detectable after the fact), and both tried to bulk_create
            # it; one loses. The whole attempt rolled back together,
            # INCLUDING the bulk UPDATE for anyone else in `remaining_ids`
            # who DID already have a row -- so they need to be retried
            # too, not just the id that collided. Do NOT narrow
            # remaining_ids here: a prior version of this function
            # narrowed to just the missing ids at this point, which
            # silently dropped the rolled-back update for anyone not in
            # that set -- found via a real threaded test that lost
            # exactly that PV with zero exceptions raised.
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


def record_personal_pv(distributor, pv_amount, today=None) -> None:
    """Credits `pv_amount` to `distributor`'s OWN monthly personal PV
    total -- distinct from record_purchase_pv, which credits PV to the
    purchasing distributor's ANCESTORS' legs, never their own record.
    Needed for the Binary/Matching Bonus "minimum personal PV this month"
    eligibility rule.

    `today` (Task 18b) lets a caller pin the exact date this credit's
    month is derived from, matching record_purchase_pv's own new `today`
    parameter -- see that function's docstring for why. Defaults to
    `timezone.now().date()` when omitted.

    Lazily creates the current month's row (mirrors PvLedger's own
    get_or_create convention) and does not retry on lock contention
    itself -- same reasoning as apps/wallet/services.py::credit(): this
    is called from within consume_paid_starter_pack's already-retried
    locked block, and an internal retry would nest and amplify under
    contention rather than just retrying the credit."""
    if not pv_amount:
        return

    period = (today or timezone.now().date()).replace(day=1)
    row, _ = MonthlyPersonalPv.objects.get_or_create(
        distributor=distributor, period=period
    )
    MonthlyPersonalPv.objects.filter(pk=row.pk).update(pv=F("pv") + pv_amount)


def reverse_ancestor_pv(distributor, pv_amount, purchase_date) -> None:
    """Reverses `pv_amount` (originally credited via `record_purchase_pv`/
    `record_personal_pv`) across `PvLedger`, `PvDailyBucket`, and
    `MonthlyPersonalPv` for `distributor`'s ancestors (and, for
    `MonthlyPersonalPv`, `distributor` themselves).

    Extracted from `apps.orders.services.cancel_or_refund_order` (Task
    18b) for Task 19's cooling-off refund reversal -- generalized to take
    a resolved `distributor` and explicit `pv_amount`/`purchase_date`
    directly rather than an `Order`, so both callers share one
    implementation instead of two independently-maintained copies.
    `apps.orders.services._reverse_ancestor_pv` is now a thin wrapper
    that resolves an Order's purchasing distributor (handling its own
    null-customer/deleted-distributor edge cases) and delegates here.

    Precondition, same "no self-retry" contract as
    `apps.wallet.services.debit()`: must be called from inside an
    already-open `transaction.atomic()` block, itself wrapped in
    `retry_on_lock_contention` by the caller -- this function does no
    locking/retry setup of its own. Every existing caller already
    satisfies this; a new caller must too.

    `purchase_date` MUST be the exact date `record_purchase_pv`'s own
    `today` argument resolved to at credit time -- never independently
    recomputed via `timezone.now().date()` at reversal time, which could
    drift by a day at a UTC-midnight boundary between the two calls and
    silently miss the `PvDailyBucket` row the credit actually landed in.
    `record_purchase_pv`/`record_personal_pv`'s own `today` parameter
    (Task 18b) exists for exactly this reason."""
    if pv_amount <= 0:
        raise ValueError(
            f"reverse_ancestor_pv: pv_amount must be positive, got {pv_amount}"
        )

    edges = list(BinaryTreeEdge.objects.filter(descendant=distributor))
    if not edges:
        return

    left_ids = sorted(
        {e.ancestor_id for e in edges if e.leg == BinaryTreeEdge.Leg.LEFT}
    )
    right_ids = sorted(
        {e.ancestor_id for e in edges if e.leg == BinaryTreeEdge.Leg.RIGHT}
    )
    all_ancestor_ids = sorted(set(left_ids) | set(right_ids))

    # Locked one at a time, in sorted-pk order -- mirrors
    # apps.binary_tree.services.BinaryTree.place_distributor's own
    # "fixed, PK-ascending order" convention, needed so this reversal
    # actually serializes against a concurrent Binary Bonus cycle for
    # any of these ancestors (apps.commissions.services.
    # process_binary_bonus_for_distributor locks the exact same
    # Distributor row before its own read-then-write PvDailyBucket
    # consumption).
    for pk in all_ancestor_ids:
        select_for_update_nowait_if_supported(Distributor.objects.filter(pk=pk)).get()

    for field, leg, ancestor_ids in (
        ("left_leg_pv", BinaryTreeEdge.Leg.LEFT, left_ids),
        ("right_leg_pv", BinaryTreeEdge.Leg.RIGHT, right_ids),
    ):
        if not ancestor_ids:
            continue

        # PvLedger: nothing else has ever decremented it (append-only
        # until this function), so a shortfall here is a real bug (a
        # double reversal, or a pv_amount/ledger mismatch elsewhere) --
        # logged at ERROR, unlike the two expected-gap cases below.
        sufficient_ids = set(
            PvLedger.objects.filter(
                distributor_id__in=ancestor_ids, **{f"{field}__gte": pv_amount}
            ).values_list("distributor_id", flat=True)
        )
        PvLedger.objects.filter(distributor_id__in=sufficient_ids).update(
            **{field: F(field) - pv_amount}
        )
        short_ids = set(ancestor_ids) - sufficient_ids
        if short_ids:
            logger.error(
                "reverse_ancestor_pv: distributor=%s pv_amount=%s could "
                "not fully reverse PvLedger.%s (%s leg) for "
                "ancestor(s)=%s -- ledger already below pv_amount. Needs "
                "manual investigation.",
                distributor.pk,
                pv_amount,
                field,
                leg,
                sorted(short_ids),
            )

        # PvDailyBucket: a shortfall here IS an expected, accepted gap
        # (ADR-0006/ADR-0007) -- the PV may have already been consumed
        # by a completed Binary Bonus cycle, or expired past
        # PV_CARRY_FORWARD_EXPIRY_DAYS. Logged at WARNING, not raised.
        sufficient_ids = set(
            PvDailyBucket.objects.filter(
                distributor_id__in=ancestor_ids,
                leg=leg,
                date=purchase_date,
                pv__gte=pv_amount,
            ).values_list("distributor_id", flat=True)
        )
        PvDailyBucket.objects.filter(
            distributor_id__in=sufficient_ids, leg=leg, date=purchase_date
        ).update(pv=F("pv") - pv_amount)
        short_ids = set(ancestor_ids) - sufficient_ids
        if short_ids:
            logger.warning(
                "reverse_ancestor_pv: distributor=%s pv_amount=%s only "
                "reversed PvDailyBucket for %s/%s %s-leg ancestor(s) -- "
                "%s already short (PV already consumed/expired) -- "
                "accepted gap, ADR-0006/ADR-0007.",
                distributor.pk,
                pv_amount,
                len(sufficient_ids),
                len(ancestor_ids),
                leg,
                sorted(short_ids),
            )

    period = purchase_date.replace(day=1)
    personal_affected = MonthlyPersonalPv.objects.filter(
        distributor=distributor, period=period, pv__gte=pv_amount
    ).update(pv=F("pv") - pv_amount)
    if not personal_affected:
        logger.warning(
            "reverse_ancestor_pv: distributor=%s pv_amount=%s "
            "MonthlyPersonalPv reversal for period=%s was a no-op "
            "(already below pv_amount) -- accepted gap, "
            "ADR-0006/ADR-0007.",
            distributor.pk,
            pv_amount,
            period,
        )


def is_eligible_for_binary_bonus(distributor, now=None) -> bool:
    """Has `distributor` personally generated at least
    config.MIN_MONTHLY_PERSONAL_PV in the current calendar month? Reads
    the pre-aggregated MonthlyPersonalPv row only -- never recomputes
    from raw purchase history.

    `now` defaults to `timezone.now()` but the Binary Bonus cycle passes
    its own fixed `run_at` explicitly, so eligibility can't flip between
    the original attempt and a retry of what's logically the same run
    (e.g. one straddling a calendar-month boundary)."""
    period = (now or timezone.now()).date().replace(day=1)
    try:
        row = MonthlyPersonalPv.objects.get(distributor=distributor, period=period)
    except MonthlyPersonalPv.DoesNotExist:
        return False
    return row.pv >= config.MIN_MONTHLY_PERSONAL_PV


def sum_leg_pv(distributor, leg, cutoff_date):
    """The non-expired PV currently sitting in `distributor`'s `leg`
    buckets -- the authoritative "spendable" total the Binary Bonus
    cycle reads, distinct from PvLedger's all-time running total (which
    this module never touches for bonus payout purposes -- PvLedger is
    read only by apps.binary_tree.services.BinaryTree._weaker_leg's
    auto-balance placement decision and a read-only reporting display,
    never by Binary/Matching Bonus). **Not immutable** since Task 18b:
    apps.orders.services.cancel_or_refund_order reverses it on a paid
    order's cancellation/refund, matching real-world MLM returns-policy
    practice and closing a leg-inflation gaming vector. `Coalesce` guards
    the empty-leg case, where `Sum` would otherwise return NULL rather
    than 0."""
    return PvDailyBucket.objects.filter(
        distributor=distributor, leg=leg, date__gte=cutoff_date, pv__gt=0
    ).aggregate(total=Coalesce(Sum("pv"), 0))["total"]


def expire_old_pv(distributor, cutoff_date):
    """Hard-deletes any of `distributor`'s buckets older than
    `cutoff_date`, logging each one first since deletion leaves no other
    trace. A bucket dated exactly `cutoff_date` is NOT expired -- PV
    survives the full PV_CARRY_FORWARD_EXPIRY_DAYS days, so `date <
    cutoff_date` (strictly before) is the expiry condition, not `<=`."""
    expired = list(
        PvDailyBucket.objects.filter(distributor=distributor, date__lt=cutoff_date)
    )
    if not expired:
        return
    for bucket in expired:
        logger.info(
            "PV expired: distributor=%s leg=%s date=%s pv=%s",
            distributor.pk,
            bucket.leg,
            bucket.date,
            bucket.pv,
        )
    PvDailyBucket.objects.filter(pk__in=[b.pk for b in expired]).delete()


def consume_leg_pv_fifo(distributor, leg, cutoff_date, amount, reference):
    """Decrements `amount` PV total from `distributor`'s `leg` buckets,
    oldest date first, in one bulk statement regardless of how many
    dated buckets are touched -- a `Case`/`When` F()-relative UPDATE
    (still the Django ORM, not raw SQL), matching this project's
    established "bulk, not per-row" convention for anything on the
    purchase/PV path (see record_purchase_pv). Using `F()` per row
    (rather than a literal computed value) means a concurrent purchase
    crediting one of these same buckets mid-consumption is never
    clobbered -- the decrement always applies relative to whatever the
    row's live value is at UPDATE time.

    Only ever called with `amount` derived from this same module's
    sum_leg_pv reading, taken under the same lock/transaction (see
    apps.commissions.services.process_binary_bonus_for_distributor) --
    so `amount` should never exceed what's actually available. Raises
    RuntimeError if it somehow does: that means a real bug elsewhere
    (e.g. leg/cutoff mismatch between the sum and the consume calls),
    not a normal condition, and money-adjacent code should fail loudly
    rather than silently under- or over-consume."""
    remaining = amount
    decrements = {}
    for bucket in (
        PvDailyBucket.objects.filter(
            distributor=distributor, leg=leg, date__gte=cutoff_date, pv__gt=0
        )
        .order_by("date")
        .only("pk", "pv")
    ):
        if remaining <= 0:
            break
        take = min(bucket.pv, remaining)
        decrements[bucket.pk] = take
        remaining -= take

    if remaining > 0:
        raise RuntimeError(
            f"consume_leg_pv_fifo: could not consume all PV for "
            f"distributor={distributor.pk} leg={leg}: {remaining} left "
            "over after exhausting all buckets -- needs manual "
            "investigation."
        )

    if not decrements:
        return

    logger.info(
        "PV consumed: distributor=%s leg=%s reference=%s decrements=%s",
        distributor.pk,
        leg,
        reference,
        decrements,
    )
    # output_field is required here, not optional: Django only infers it
    # automatically when every branch resolves to the same field type,
    # and "complex expressions that mix field types" (each F("pv") - take
    # here is a plain-int-typed subtraction, ambiguous against the
    # model's PositiveIntegerField column across multiple When branches)
    # need it stated explicitly, or Django raises FieldError.
    # https://docs.djangoproject.com/en/5.0/ref/models/expressions/#output-field
    case_expr = Case(
        *[When(pk=pk, then=F("pv") - take) for pk, take in decrements.items()],
        default=F("pv"),
        output_field=PositiveIntegerField(),
    )
    PvDailyBucket.objects.filter(pk__in=list(decrements.keys())).update(pv=case_expr)


def distributor_ids_with_pending_pv():
    """Distributor ids with at least one non-zero PvDailyBucket row --
    the set a Binary Bonus batch driver (not yet built) should iterate,
    instead of every registered Distributor. Most registered users are
    customers with zero binary-tree activity, and even among
    distributors, most have no outstanding PV in any given cycle once
    consumed or expired -- scanning the whole distributor table every
    BINARY_BONUS_INTERVAL_MINUTES would be exactly the kind of
    unbounded-with-scale cost SPEC.md's Scale Architecture already
    eliminated once for the write path (see record_purchase_pv); this is
    the equivalent guard for the read/payout path.

    Excludes rows left at pv=0 by consume_leg_pv_fifo's partial-
    consumption boundary case -- those have nothing left to pay out and
    would just cost the batch driver a wasted evaluation."""
    return (
        PvDailyBucket.objects.filter(pv__gt=0)
        .values_list("distributor_id", flat=True)
        .distinct()
    )
