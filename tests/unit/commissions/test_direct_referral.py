from decimal import Decimal

import pytest

from apps.commissions.services import calculate_direct_referral_bonus


@pytest.mark.django_db
def test_pack_a_purchase_earns_ghs_50():
    """Confirmed 2026-07-14: rate x PV, treating 1 PV as GHS 1. Pack A is
    500 PV -- see tasks/todo.md Task 12 and the
    project_direct_referral_bonus_formula memory for why this isn't the
    GHS 75 figure in the original requirements doc's Section 14 (that
    number doesn't reconcile with Section 14's own Pack B example under
    any single consistent formula)."""
    assert calculate_direct_referral_bonus(pv=500) == Decimal("50.00")


@pytest.mark.django_db
def test_pack_b_purchase_earns_ghs_100():
    assert calculate_direct_referral_bonus(pv=1000) == Decimal("100.00")


@pytest.mark.django_db
def test_rounds_to_the_pesewa_on_a_non_round_value():
    """500 PV x 10.333% = 51.665 exactly -- a genuine half-pesewa case,
    proving rounding is ROUND_HALF_UP (rounds to 51.67, not 51.66). The
    default 10% rate never produces a third decimal digit on its own, so
    this overrides the rate via constance to force a real rounding case."""
    from constance import config

    original_rate = config.DIRECT_REFERRAL_BONUS_RATE
    config.DIRECT_REFERRAL_BONUS_RATE = Decimal("10.333")
    try:
        assert calculate_direct_referral_bonus(pv=500) == Decimal("51.67")
    finally:
        config.DIRECT_REFERRAL_BONUS_RATE = original_rate
