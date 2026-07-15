import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from constance import config

from apps.binary_tree.models import BinaryTreeEdge
from apps.distributors.models import Distributor
from apps.pv_ledger.services import (
    consume_leg_pv_fifo,
    expire_old_pv,
    is_eligible_for_binary_bonus,
    sum_leg_pv,
)
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit
from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

logger = logging.getLogger(__name__)


def calculate_direct_referral_bonus(pv: int) -> Decimal:
    """DIRECT_REFERRAL_BONUS_RATE% x PV, treating 1 PV as GHS 1 -- confirmed
    2026-07-14 (see tasks/todo.md Task 12 and the
    project_direct_referral_bonus_formula memory; the original
    requirements doc's Section 14 worked examples don't reconcile with a
    single consistent formula, so this was a decision, not a spec lookup).
    Rounded to the pesewa (2 decimal places, half-up) -- the first place
    in this codebase money rounding is needed, so this establishes the
    convention.

    A plain function, not a `DirectReferralCalculator` class as originally
    planned -- there's no shared state or multi-step logic here to justify
    one. Binary/Matching Bonus (Tasks 13/14) may still end up as classes
    if their weak-leg/cap/carry-forward or downline-traversal logic turns
    out to warrant it; don't force this function into a class just to
    match their names if that's what they become.
    """
    rate = config.DIRECT_REFERRAL_BONUS_RATE
    amount = (Decimal(pv) * rate) / Decimal("100")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def calculate_binary_bonus(weak_leg_pv: int) -> Decimal:
    """BINARY_BONUS_RATE% x weak_leg_pv, treating 1 PV as GHS 1 -- same
    convention as calculate_direct_referral_bonus, rounded to the pesewa
    (half-up). Pure calculation only: this does not reset legs, apply the
    weekly cap (see apply_weekly_binary_bonus_cap), or handle carry-
    forward -- those are the Celery task's job (Task 13c/13d), not this
    function's."""
    rate = config.BINARY_BONUS_RATE
    amount = (Decimal(weak_leg_pv) * rate) / Decimal("100")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def apply_weekly_binary_bonus_cap(distributor, raw_bonus: Decimal, now=None) -> Decimal:
    """Reduces `raw_bonus` so that (BINARY_BONUS already paid to
    `distributor` in the last 7 days) + (the returned amount) never
    exceeds config.WEEKLY_BINARY_BONUS_CAP. Never returns a negative
    amount -- already at or past the cap returns Decimal("0.00").

    Uses a rolling 7-day window, not a calendar week -- the source spec
    doesn't define calendar-week boundaries (Monday-Sunday vs Sunday-
    Saturday, which timezone), and a rolling window avoids a distributor
    getting 2x the cap by earning right at a calendar-week boundary.
    Only BINARY_BONUS-type transactions count toward this specific cap --
    other bonus types have their own eligibility rules, not this one.

    `now` defaults to `timezone.now()` but the Binary Bonus cycle passes
    its own fixed `run_at` explicitly, so the cap window can't shift
    between the original attempt and a retry of what's logically the
    same run."""
    cutoff = (now or timezone.now()) - timedelta(days=7)
    already_paid = WalletTransaction.objects.filter(
        wallet__distributor=distributor,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        created_at__gte=cutoff,
    ).aggregate(total=Coalesce(Sum("amount"), Decimal("0")))["total"]

    remaining_room = config.WEEKLY_BINARY_BONUS_CAP - already_paid
    if remaining_room <= 0:
        return Decimal("0.00")
    return min(raw_bonus, remaining_room)


def process_binary_bonus_for_distributor(distributor, run_at) -> Decimal:
    """Runs one Binary Bonus payout cycle for `distributor`, returning
    the amount actually credited (possibly `Decimal("0.00")`). Called
    once per distributor per scheduled cycle run (e.g. every
    BINARY_BONUS_INTERVAL_MINUTES) by a batch driver not yet built.

    `run_at` MUST be the SAME fixed timestamp passed for every
    distributor processed within one cycle run -- never re-read per
    distributor, and never regenerated on retry of what is logically the
    same cycle. It is the sole idempotency key (via `reference` below),
    and both eligibility and the weekly cap are evaluated as of this
    same instant so a retried attempt can't see a different verdict at a
    month/week boundary than the original attempt did. Whichever batch
    driver eventually calls this across the whole distributor base must
    uphold this contract.

    Locks the Distributor row FIRST, before even checking whether this
    cycle already ran -- this fully serializes concurrent or retried
    invocations for the SAME distributor, rather than letting the
    idempotency check race a concurrent attempt via an unlocked read.
    It's what makes `credit()`'s unique-constraint IntegrityError
    unreachable in practice: a second, later attempt for the same
    `(distributor, run_at)` blocks until the first's transaction fully
    commits or rolls back, then sees the first's already-committed
    WalletTransaction via the check below and returns early instead of
    ever calling `credit()` a second time for the same reference.

    Never touches PvLedger (an immutable all-time historical total) --
    reads/writes only PvDailyBucket, via `sum_leg_pv` / `expire_old_pv` /
    `consume_leg_pv_fifo`. PvDailyBucket.pv is only ever INCREASED
    outside this function (by the write-time purchase-credit path in
    apps.pv_ledger.services.record_purchase_pv) and only ever DECREASED
    by this function -- so a concurrent purchase landing mid-cycle for
    this same distributor can only make MORE PV available than this
    cycle's totals counted, never less, and `consume_leg_pv_fifo` never
    needs to (and structurally cannot) reach into PV a concurrent
    purchase just added, since it never consumes more than what
    `sum_leg_pv` already counted for that leg.

    If the weekly cap reduces the payout below the raw calculated bonus,
    only the proportional PV actually monetized this cycle is consumed
    (floored, never rounded up, so PV is never "spent" without being
    paid for) -- the remainder stays in the buckets for a future cycle.

    Raises `RuntimeError` if `consume_leg_pv_fifo`'s internal invariant
    is violated (a real bug elsewhere, not a normal condition) and lets
    `OperationalError` propagate if `retry_on_lock_contention` exhausts
    its retries. Either way nothing commits for this distributor --
    whatever batch driver calls this once per distributor must catch
    exceptions per-distributor so one distributor's failure can't abort
    the whole cycle for everyone else."""
    reference = f"binary-bonus-{distributor.pk}-{run_at.isoformat()}"

    def _attempt():
        with transaction.atomic():
            select_for_update_nowait_if_supported(
                Distributor.objects.filter(pk=distributor.pk)
            ).get()

            existing = WalletTransaction.objects.filter(
                wallet__distributor=distributor,
                transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
                reference=reference,
            ).first()
            if existing is not None:
                return existing.amount

            # Expiry runs unconditionally, before the eligibility check --
            # a chronically-ineligible distributor's PvDailyBucket rows
            # must still age out on schedule, not accumulate forever just
            # because they never qualify for a payout.
            # Expiry runs unconditionally, before the eligibility check --
            # a chronically-ineligible distributor'''s PvDailyBucket rows
            # must still age out on schedule, not accumulate forever just
            # because they never qualify for a payout.
            cutoff_date = run_at.date() - timedelta(
                days=config.PV_CARRY_FORWARD_EXPIRY_DAYS
            )
            expire_old_pv(distributor, cutoff_date)

            if not is_eligible_for_binary_bonus(distributor, now=run_at):
                return Decimal("0.00")

            left_pv = sum_leg_pv(distributor, BinaryTreeEdge.Leg.LEFT, cutoff_date)
            right_pv = sum_leg_pv(distributor, BinaryTreeEdge.Leg.RIGHT, cutoff_date)
            weak_leg_pv = min(left_pv, right_pv)

            if weak_leg_pv <= 0:
                return Decimal("0.00")

            raw_bonus = calculate_binary_bonus(weak_leg_pv)
            actual_bonus = apply_weekly_binary_bonus_cap(
                distributor, raw_bonus, now=run_at
            )

            if actual_bonus <= 0:
                return Decimal("0.00")

            if actual_bonus >= raw_bonus:
                pv_to_consume = weak_leg_pv
            else:
                pv_to_consume = int(weak_leg_pv * actual_bonus / raw_bonus)

            if pv_to_consume <= 0:
                # actual_bonus is nonzero (the cap left SOME room) but
                # too small relative to raw_bonus to floor to even 1 PV.
                # Deliberately deferred, not paid: paying it now with 0
                # PV consumed would leave the SAME weak_leg_pv available
                # next cycle too, letting further tiny slivers accrue
                # against PV that was never actually marked spent -- a
                # real double-pay risk. Logged so this is distinguishable
                # from the ordinary "nothing owed" cases above.
                logger.info(
                    "process_binary_bonus_for_distributor: distributor=%s "
                    "owed actual_bonus=%s this cycle but it floors to 0 "
                    "PV (weak_leg_pv=%s raw_bonus=%s) -- deferring to a "
                    "future cycle rather than paying without consuming.",
                    distributor.pk,
                    actual_bonus,
                    weak_leg_pv,
                    raw_bonus,
                )
                return Decimal("0.00")

            credit(
                distributor,
                actual_bonus,
                transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
                reference=reference,
            )
            consume_leg_pv_fifo(
                distributor,
                BinaryTreeEdge.Leg.LEFT,
                cutoff_date,
                pv_to_consume,
                reference,
            )
            consume_leg_pv_fifo(
                distributor,
                BinaryTreeEdge.Leg.RIGHT,
                cutoff_date,
                pv_to_consume,
                reference,
            )

            return actual_bonus

    return retry_on_lock_contention(_attempt)
