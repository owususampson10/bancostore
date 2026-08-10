import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Exists, OuterRef, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from constance import config

from apps.binary_tree.models import BinaryTreeEdge
from apps.distributors.models import Distributor
from apps.notifications.models import Notification
from apps.notifications.services import send_notification
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

    Never touches PvLedger -- reads/writes only PvDailyBucket, via
    `sum_leg_pv` / `expire_old_pv` / `consume_leg_pv_fifo`. (PvLedger is
    read only by apps.binary_tree.services.BinaryTree._weaker_leg's
    auto-balance placement decision and a reporting display, and since
    Task 18b is no longer immutable -- apps.orders.services.
    cancel_or_refund_order reverses it on a paid order's cancellation/
    refund.) PvDailyBucket.pv was only ever INCREASED outside this
    function (by the write-time purchase-credit path in
    apps.pv_ledger.services.record_purchase_pv) and only ever DECREASED
    by this function, UNTIL Task 18b added a second decrementer
    (cancel_or_refund_order, reversing a refunded/cancelled order's PV).
    That function locks the same ancestor Distributor row this function
    locks (below) before touching that ancestor's PvDailyBucket rows --
    the two can never interleave for the same ancestor, so the "a
    concurrent purchase landing mid-cycle can only make MORE PV
    available, never less" reasoning below still holds against
    record_purchase_pv (which takes no such lock, but only ever
    increases); it does NOT need to separately reason about
    cancel_or_refund_order, since that path is fully serialized out by
    the shared lock instead.

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
            cutoff_date = run_at.date() - timedelta(
                days=config.PV_CARRY_FORWARD_EXPIRY_DAYS
            )
            expire_old_pv(distributor, cutoff_date)

            if not is_eligible_for_binary_bonus(distributor, now=run_at):
                # DEBUG, not INFO: this is the routine, expected outcome
                # for most distributors most cycles (this runs once per
                # distributor every BINARY_BONUS_INTERVAL_MINUTES), so at
                # INFO it would flood production logs. Kept so a support
                # inquiry ("why wasn't I paid?") can raise verbosity for
                # one distributor and get a real answer instead of
                # nothing -- the same reasoning as every DEBUG line below.
                logger.debug(
                    "process_binary_bonus_for_distributor: distributor=%s "
                    "skipped this cycle -- not eligible (insufficient "
                    "monthly personal PV) as of run_at=%s.",
                    distributor.pk,
                    run_at,
                )
                return Decimal("0.00")

            left_pv = sum_leg_pv(distributor, BinaryTreeEdge.Leg.LEFT, cutoff_date)
            right_pv = sum_leg_pv(distributor, BinaryTreeEdge.Leg.RIGHT, cutoff_date)
            weak_leg_pv = min(left_pv, right_pv)

            if weak_leg_pv <= 0:
                logger.debug(
                    "process_binary_bonus_for_distributor: distributor=%s "
                    "skipped this cycle -- zero weak-leg PV "
                    "(left_pv=%s right_pv=%s).",
                    distributor.pk,
                    left_pv,
                    right_pv,
                )
                return Decimal("0.00")

            raw_bonus = calculate_binary_bonus(weak_leg_pv)
            actual_bonus = apply_weekly_binary_bonus_cap(
                distributor, raw_bonus, now=run_at
            )

            if actual_bonus <= 0:
                logger.debug(
                    "process_binary_bonus_for_distributor: distributor=%s "
                    "skipped this cycle -- weekly cap already exhausted "
                    "(weak_leg_pv=%s raw_bonus=%s).",
                    distributor.pk,
                    weak_leg_pv,
                    raw_bonus,
                )
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
            # Task 21d-ii: Section 6.6's "a binary bonus was calculated
            # and credited". Matching bonus deliberately has no
            # equivalent call anywhere -- Section 6.6 never names it.
            send_notification(
                distributor,
                Notification.EventType.BINARY_BONUS_CREDITED,
                f"You earned GHS {actual_bonus} Binary Bonus!",
            )

            return actual_bonus

    return retry_on_lock_contention(_attempt)


def calculate_matching_bonus(total_downline_earnings: Decimal) -> Decimal:
    """MATCHING_BONUS_RATE% x total_downline_earnings, same rounding
    convention as calculate_binary_bonus/calculate_direct_referral_bonus
    (Decimal.quantize(..., ROUND_HALF_UP) to the pesewa). Applied once to
    the combined total across all downline levels, not per-level then
    summed -- mathematically identical for a single constant rate
    (confirmed against the doc example: 5% x (200+150+100) = 10+7.50+5),
    and simpler to compute as one combined sum (see
    sum_downline_binary_bonus_earnings)."""
    rate = config.MATCHING_BONUS_RATE
    amount = (total_downline_earnings * rate) / Decimal("100")
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Worst-case ceiling on how many sponsor-chain levels a single
# Silver-rank ("unlimited") walk will ever traverse, independent of
# config.MATCHING_BONUS_DEPTH_SILVER or the graph's actual shape.
# 2026-07-22 security-and-hardening review: this task's Redis overlap
# lock (apps/commissions/tasks.py::MATCHING_BONUS_LOCK_TIMEOUT_SECONDS)
# is only renewed BETWEEN distributors in the batch driver's loop, never
# DURING one distributor's own processing -- unlike Binary Bonus, this
# job has no weekly cap or PV-consumption defense to throttle the damage
# if two cycles ever did overlap (documented in this module's docstring:
# "No leg consumption, no PV expiry, no weekly cap... not omitted by
# oversight"), so an unbounded single-distributor walk that outlasted the
# lock's TTL would be a genuine, not just theoretical, double-payment
# risk. 500 is generous relative to any known/plausible real sponsor-
# chain depth while keeping worst-case single-call runtime a small
# fraction of the lock's TTL regardless of graph shape.
MAX_MATCHING_BONUS_WALK_DEPTH = 500


def walk_sponsor_chain_downline_ids(distributor, max_depth=None) -> set:
    """Cycle-safe, depth-capped BFS of `distributor`'s SPONSOR chain
    (Distributor.sponsor, the recruitment chain a starter-pack purchase or
    registration sets -- deliberately NOT apps.binary_tree's placement
    tree, which can diverge from who-recruited-whom under spillover).
    Returns the set of every downline distributor's pk, with no ordering
    guarantee.

    Extracted from sum_downline_binary_bonus_earnings (Task 14) so Task 33's
    Team page can reuse the exact same walk instead of a second
    implementation of the cycle guard below -- a behavior-preserving
    extraction, not a new algorithm; sum_downline_binary_bonus_earnings's
    own test suite (tests/unit/commissions/test_matching_bonus.py) already
    covers cycle-safety and the depth ceiling and continues to pass
    unchanged against this version.

    `max_depth` is levels deep to walk: None means unlimited, 0 means don't
    walk at all (returns an empty set immediately).

    One bulk query per level (Distributor.objects.filter(sponsor_id__in=
    [...])), never one query per distributor, matching this codebase's
    established "never one query per ancestor" convention (see
    apps.pv_ledger.services.record_purchase_pv).

    Guards against sponsor-chain cycles explicitly: unlike
    apps.binary_tree.BinaryTreeEdge (which has a DB constraint banning
    ancestor == descendant), Distributor.sponsor is a plain self-FK with
    no such protection. A corrupted graph (e.g. a data-entry bug that
    reassigns an ancestor's sponsor back to one of their own descendants)
    would infinite-loop an unguarded "walk until a level is empty" -- each
    level's query here explicitly excludes every id already counted in a
    prior level, so even a cyclic graph terminates (the cycle's ids are
    all consumed by the level that first reaches them, leaving nothing
    new for the next iteration to find).

    Also hard-capped at MAX_MATCHING_BONUS_WALK_DEPTH regardless of
    max_depth or the graph's real size -- see that constant's own
    docstring for why this matters even with the cycle guard above (a
    cycle-free but pathologically deep chain is a different risk than an
    infinite loop, and the cycle guard alone doesn't bound it). The name
    keeps its original Matching-Bonus-specific prefix since that's still
    where the constant is defined and tuned; Task 33 reuses the same value
    rather than defining a second, possibly-diverging ceiling."""
    if max_depth == 0:
        return set()

    all_downline_ids = set()
    current_level_ids = [distributor.pk]
    depth = 0
    while current_level_ids:
        if max_depth is not None and depth >= max_depth:
            break
        if depth >= MAX_MATCHING_BONUS_WALK_DEPTH:
            logger.warning(
                "walk_sponsor_chain_downline_ids: distributor=%s hit "
                "the %s-level walk ceiling before its sponsor chain ran "
                "out -- returning only what was found in the first %s "
                "levels. Needs investigation if this fires in practice "
                "(either a pathologically deep real chain, or a graph "
                "issue the cycle guard doesn't catch).",
                distributor.pk,
                MAX_MATCHING_BONUS_WALK_DEPTH,
                MAX_MATCHING_BONUS_WALK_DEPTH,
            )
            break
        next_level_ids = list(
            Distributor.objects.filter(sponsor_id__in=current_level_ids)
            .exclude(pk__in=all_downline_ids | {distributor.pk})
            .values_list("pk", flat=True)
        )
        if not next_level_ids:
            break
        all_downline_ids.update(next_level_ids)
        current_level_ids = next_level_ids
        depth += 1

    return all_downline_ids


def sum_downline_binary_bonus_earnings(distributor, max_depth, run_at) -> Decimal:
    """Sums every member of `distributor`'s sponsor-chain downline's own
    BINARY_BONUS-type WalletTransaction credits from the rolling
    `config.MATCHING_BONUS_INTERVAL_DAYS` days before `run_at` (not a
    calendar week -- same "undefined calendar-week boundary" reasoning
    apply_weekly_binary_bonus_cap already documents for the identical
    ambiguity). The downline set itself comes from
    walk_sponsor_chain_downline_ids -- see that function's docstring for
    the walk/cycle-guard/depth-cap details this function no longer needs
    to restate.

    The window tracks the live cadence setting, not a hardcoded 7 --
    CodeRabbit review, 2026-07-22: an earlier version hardcoded
    timedelta(days=7) while MATCHING_BONUS_INTERVAL_DAYS (the admin-
    editable cycle cadence this same window is meant to cover) could
    already be changed independently. If an admin lowers the interval
    below the window, consecutive cycles would both count the same
    downline earnings (over-payment, with no cap to absorb it, per this
    module's own "no weekly cap ... not omitted by oversight" note); if
    they raise it, earnings older than the window would never get
    matched (silent under-payment). Reading the live setting here keeps
    the window and the cadence structurally unable to diverge.

    `max_depth` is levels deep to walk: None means unlimited (Silver
    rank), 0 means don't walk at all (any distributor whose rank isn't
    bronze or silver -- including a blank rank, i.e. no starter pack
    purchased yet -- gets no matching bonus). Bronze's cap comes from
    config.MATCHING_BONUS_DEPTH_BRONZE at the caller (see
    process_matching_bonus_for_distributor), not hardcoded here -- this
    function only knows "how many levels," not "which rank means what."."""
    all_downline_ids = walk_sponsor_chain_downline_ids(distributor, max_depth)

    if not all_downline_ids:
        return Decimal("0.00")

    cutoff = run_at - timedelta(days=config.MATCHING_BONUS_INTERVAL_DAYS)
    return WalletTransaction.objects.filter(
        wallet__distributor_id__in=all_downline_ids,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        created_at__gte=cutoff,
        created_at__lt=run_at,
    ).aggregate(total=Coalesce(Sum("amount"), Decimal("0")))["total"]


MATCHING_BONUS_RANKS = ("bronze", "silver")


def _matching_bonus_depth_for_rank(rank):
    """Maps a distributor's rank to how many sponsor-chain levels
    sum_downline_binary_bonus_earnings should walk -- None means
    unlimited, 0 means no matching bonus at all. Any rank not in
    MATCHING_BONUS_RANKS -- including "" (no starter pack purchased yet)
    or a future rank this mapping was never updated for -- gets 0, not a
    crash or a guess.

    Both config.MATCHING_BONUS_DEPTH_BRONZE and _DEPTH_SILVER are read
    live here (2026-07-22 doubt-driven-development review caught that an
    earlier draft hardcoded Silver's depth as always-unlimited, ignoring
    _DEPTH_SILVER entirely -- the exact "decorative constance setting"
    bug class already found and fixed once for BINARY_BONUS_INTERVAL_
    MINUTES). _DEPTH_SILVER's own documented convention (see
    apps/platform_settings/config.py) is 0 = unlimited, matching its
    help text, not a hardcoded None here."""
    if rank == "silver":
        depth = config.MATCHING_BONUS_DEPTH_SILVER
        return None if depth == 0 else depth
    if rank == "bronze":
        return config.MATCHING_BONUS_DEPTH_BRONZE
    return 0


def distributor_ids_eligible_for_matching_bonus():
    """Distributor ids worth evaluating for Matching Bonus this cycle --
    the set a batch driver should iterate instead of every registered
    Distributor, matching apps.pv_ledger.services.distributor_ids_with_
    pending_pv's role for Binary Bonus. Filters to a matching-bonus-
    eligible rank (MATCHING_BONUS_RANKS, the same source
    _matching_bonus_depth_for_rank reads -- a single list, not two
    independently-hardcoded ones that could drift apart) AND at least one
    direct referral (`referrals` is Distributor.sponsor's reverse FK
    related_name) -- a distributor with the right rank but nobody in
    their downline can never owe anything, so there's no point evaluating
    them every cycle. This is only a pre-filter, not the full eligibility
    check (monthly personal PV, and whether that downline actually earned
    anything in the past 7 days) -- those still happen per-distributor in
    process_matching_bonus_for_distributor, since determining them cheaply
    without doing that same work isn't possible.

    Exists(), not a join + distinct() -- a self-join on `referrals` (the
    reverse FK) followed by DISTINCT was the original shape, but it forces
    a sort/dedupe over what's effectively a semi-join at this platform's
    stated scale (code-review finding, 2026-07-22). Exists() compiles to
    a correlated subquery instead: cheaper, and correct without needing
    DISTINCT at all, since it only ever tests presence, never joins rows
    in."""
    has_referral = Distributor.objects.filter(sponsor_id=OuterRef("pk"))
    return (
        Distributor.objects.filter(rank__in=MATCHING_BONUS_RANKS)
        .filter(Exists(has_referral))
        .values_list("pk", flat=True)
    )


def process_matching_bonus_for_distributor(distributor, run_at) -> Decimal:
    """Runs one Matching Bonus payout cycle for `distributor`, returning
    the amount actually credited (possibly `Decimal("0.00")`). Mirrors
    process_binary_bonus_for_distributor's contract -- `run_at` MUST be
    the SAME fixed timestamp passed for every distributor processed
    within one cycle run (the sole idempotency key, via `reference`
    below), and this locks the Distributor row FIRST for the same
    same-distributor-retry-serialization reason documented there.

    Unlike Binary Bonus, this never mutates any OTHER distributor's data
    -- it only READS downline members' already-committed WalletTransaction
    rows (via sum_downline_binary_bonus_earnings) and credits `distributor`
    their own wallet. No leg consumption, no PV expiry, no weekly cap:
    none of those exist for Matching Bonus per its spec (tasks/todo.md's
    Task 14 note) -- not omitted by oversight.

    Deliberately re-reads every field it needs (rank included) off the
    row this function itself locks, never off the `distributor` argument
    as passed in. A batch driver may pass an unsaved Distributor(pk=id)
    stub (exactly like calculate_binary_bonus already does, since
    process_binary_bonus_for_distributor only ever needs .pk) -- but
    THIS function needs .rank, and a stub's rank is always "" (the
    model's default), never the real value. A 2026-07-22 doubt-driven-
    development review caught an earlier draft that discarded the locked
    row's data and read .rank off the passed-in argument instead --
    which would have silently paid nobody, forever, since every stub's
    rank resolves to "no matching bonus tier" with no exception raised
    anywhere. Confirmed by re-reading this exact function, not assumed
    fixed."""
    reference = f"matching-bonus-{distributor.pk}-{run_at.isoformat()}"

    def _attempt():
        with transaction.atomic():
            locked_distributor = select_for_update_nowait_if_supported(
                Distributor.objects.filter(pk=distributor.pk)
            ).get()

            existing = WalletTransaction.objects.filter(
                wallet__distributor=locked_distributor,
                transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS,
                reference=reference,
            ).first()
            if existing is not None:
                return existing.amount

            if not is_eligible_for_binary_bonus(locked_distributor, now=run_at):
                logger.debug(
                    "process_matching_bonus_for_distributor: distributor=%s "
                    "skipped this cycle -- not eligible (insufficient "
                    "monthly personal PV) as of run_at=%s.",
                    locked_distributor.pk,
                    run_at,
                )
                return Decimal("0.00")

            max_depth = _matching_bonus_depth_for_rank(locked_distributor.rank)
            if max_depth == 0:
                logger.debug(
                    "process_matching_bonus_for_distributor: distributor=%s "
                    "skipped this cycle -- rank=%r has no matching bonus "
                    "tier.",
                    locked_distributor.pk,
                    locked_distributor.rank,
                )
                return Decimal("0.00")

            total_downline_earnings = sum_downline_binary_bonus_earnings(
                locked_distributor, max_depth, run_at
            )
            if total_downline_earnings <= 0:
                logger.debug(
                    "process_matching_bonus_for_distributor: distributor=%s "
                    "skipped this cycle -- zero downline binary-bonus "
                    "earnings in the past 7 days.",
                    locked_distributor.pk,
                )
                return Decimal("0.00")

            bonus = calculate_matching_bonus(total_downline_earnings)
            if bonus <= 0:
                return Decimal("0.00")

            credit(
                locked_distributor,
                bonus,
                transaction_type=WalletTransaction.TransactionType.MATCHING_BONUS,
                reference=reference,
            )
            return bonus

    return retry_on_lock_contention(_attempt)
