from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Sum
from django.utils import timezone

from constance import config

from apps.wallet.models import WalletTransaction


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


def apply_weekly_binary_bonus_cap(distributor, raw_bonus: Decimal) -> Decimal:
    """Reduces `raw_bonus` so that (BINARY_BONUS already paid to
    `distributor` in the last 7 days) + (the returned amount) never
    exceeds config.WEEKLY_BINARY_BONUS_CAP. Never returns a negative
    amount -- already at or past the cap returns Decimal("0.00").

    Uses a rolling 7-day window, not a calendar week -- the source spec
    doesn't define calendar-week boundaries (Monday-Sunday vs Sunday-
    Saturday, which timezone), and a rolling window avoids a distributor
    getting 2x the cap by earning right at a calendar-week boundary.
    Only BINARY_BONUS-type transactions count toward this specific cap --
    other bonus types have their own eligibility rules, not this one."""
    cutoff = timezone.now() - timedelta(days=7)
    already_paid = WalletTransaction.objects.filter(
        wallet__distributor=distributor,
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        created_at__gte=cutoff,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0")

    remaining_room = config.WEEKLY_BINARY_BONUS_CAP - already_paid
    if remaining_room <= 0:
        return Decimal("0.00")
    return min(raw_bonus, remaining_room)
