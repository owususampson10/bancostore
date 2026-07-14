from decimal import ROUND_HALF_UP, Decimal

from constance import config


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
