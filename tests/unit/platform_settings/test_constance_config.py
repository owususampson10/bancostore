from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.module_loading import import_string

import pytest
from constance import config


def _build_additional_field(name):
    """Mirrors how constance.admin.ConstanceForm resolves a
    CONSTANCE_ADDITIONAL_FIELDS entry -- builds the real form field
    directly, in isolation from the admin form's version-hash/CSRF-style
    machinery, so these tests exercise exactly the min/max validation this
    project added and nothing else.

    constance.admin resolves this project's string paths (config.py's
    documented, import-order-safe convention) into real classes/instances
    the first time it's imported, mutating settings.CONSTANCE_ADDITIONAL_
    FIELDS in place -- confirmed via a real shell session, not assumed.
    Handles both forms since which one this sees depends on whether
    constance.admin has already been imported by the time a given test
    runs (Django admin autodiscovery order), not on anything these tests
    control."""
    field_path, kwargs = settings.CONSTANCE_ADDITIONAL_FIELDS[name]
    field_class = (
        import_string(field_path) if isinstance(field_path, str) else field_path
    )
    kwargs = dict(kwargs)
    widget = kwargs.get("widget")
    if isinstance(widget, str):
        kwargs["widget"] = import_string(widget)
    return field_class(**kwargs)


@pytest.mark.django_db
def test_binary_bonus_rate_default():
    assert config.BINARY_BONUS_RATE == 7.5


@pytest.mark.django_db
def test_updating_setting_takes_effect_on_next_read():
    config.BINARY_BONUS_RATE = Decimal("9")

    assert config.BINARY_BONUS_RATE == Decimal("9")


def test_every_config_key_belongs_to_exactly_one_fieldset():
    fieldset_keys = [
        key for keys in settings.CONSTANCE_CONFIG_FIELDSETS.values() for key in keys
    ]

    assert sorted(fieldset_keys) == sorted(settings.CONSTANCE_CONFIG.keys())
    assert len(fieldset_keys) == len(set(fieldset_keys))


def test_constance_uses_the_redis_cache_backend():
    assert settings.CONSTANCE_DATABASE_CACHE_BACKEND == "default"


@pytest.mark.django_db
def test_section_15_key_rules_summary_defaults():
    """Every Section 15 rule whose 'Controlled In' column is in scope for Task 3
    (13.1-13.4, 13.9, 13.13-13.15 per SPEC.md). The 70% Retail Rule (13.12) and
    Delivery Fees (13.5) are out of scope and stubbed elsewhere, per SPEC.md."""
    assert config.REGISTRATION_FEE == Decimal("100")
    assert config.STARTER_PACK_A_PRICE == Decimal("1500")
    assert config.STARTER_PACK_A_PV == 500
    assert config.STARTER_PACK_A_RANK == "bronze"
    assert config.STARTER_PACK_B_PRICE == Decimal("2000")
    assert config.STARTER_PACK_B_PV == 1000
    assert config.STARTER_PACK_B_RANK == "silver"
    assert config.DIRECT_REFERRAL_BONUS_RATE == Decimal("10")
    assert config.BINARY_BONUS_RATE == Decimal("7.5")
    assert config.WEEKLY_BINARY_BONUS_CAP == Decimal("50000")
    assert config.MATCHING_BONUS_RATE == Decimal("5")
    assert config.MATCHING_BONUS_DEPTH_BRONZE == 3
    assert config.MATCHING_BONUS_DEPTH_SILVER == 0  # 0 = unlimited
    assert config.MIN_MONTHLY_PERSONAL_PV == 100
    assert config.PV_CARRY_FORWARD_EXPIRY_DAYS == 180
    assert config.WITHDRAWAL_FREQUENCY == "weekly"
    assert config.WITHHOLDING_TAX_RATE == Decimal("1")
    assert config.KYC_REQUIRED is True
    assert config.COOLING_OFF_PERIOD_DAYS == 7
    assert config.COOLING_OFF_REFUND_DEDUCTION_RATE == Decimal("10")


def test_the_three_money_affecting_keys_reference_their_bounded_fields():
    """2026-07-22 security-and-hardening review: BINARY_BONUS_RATE,
    WEEKLY_BINARY_BONUS_CAP, and BINARY_BONUS_INTERVAL_MINUTES had no
    admin-form bounds -- a single fat-fingered value (e.g. 750 instead of
    7.5) took effect immediately with no review step. Scoped to exactly
    these three keys (the ones named in that review's findings for the
    Binary Bonus batch driver) -- other rate/cap settings elsewhere in this
    config are a separate decision, not made here."""
    assert settings.CONSTANCE_CONFIG["BINARY_BONUS_RATE"][2] == "percentage_field"
    assert (
        settings.CONSTANCE_CONFIG["WEEKLY_BINARY_BONUS_CAP"][2]
        == "non_negative_money_field"
    )
    assert (
        settings.CONSTANCE_CONFIG["BINARY_BONUS_INTERVAL_MINUTES"][2]
        == "interval_minutes_field"
    )


def test_binary_bonus_rate_field_rejects_a_fat_fingered_750():
    field = _build_additional_field("percentage_field")
    with pytest.raises(ValidationError):
        field.clean("750")


def test_binary_bonus_rate_field_accepts_the_seeded_default():
    field = _build_additional_field("percentage_field")
    assert field.clean("7.5") == Decimal("7.5")


def test_binary_bonus_rate_field_rejects_negative():
    field = _build_additional_field("percentage_field")
    with pytest.raises(ValidationError):
        field.clean("-1")


def test_binary_bonus_rate_field_accepts_zero_as_a_deliberate_pause_lever():
    """Same reasoning as WEEKLY_BINARY_BONUS_CAP's zero-acceptance test
    below: a rate of 0 already pays nothing with no crash (see
    tests/unit/commissions/test_binary_bonus_task.py's
    test_a_zero_rate_pauses_bonus_accrual_without_erroring for the
    end-to-end proof), so this is a legitimate lever, not an oversight."""
    field = _build_additional_field("percentage_field")
    assert field.clean("0") == Decimal("0")


def test_weekly_binary_bonus_cap_field_rejects_negative():
    field = _build_additional_field("non_negative_money_field")
    with pytest.raises(ValidationError):
        field.clean("-100")


def test_weekly_binary_bonus_cap_field_accepts_zero_as_a_deliberate_pause_lever():
    """apply_weekly_binary_bonus_cap already treats a cap of 0 as 'no room,
    pay nothing' with no crash (services.py's remaining_room <= 0 branch) --
    that's a legitimate incident-response lever (pause binary bonus payouts
    without touching the rate or disabling the whole job), so the floor is
    0, not 0.01."""
    field = _build_additional_field("non_negative_money_field")
    assert field.clean("0") == Decimal("0")


def test_binary_bonus_interval_field_rejects_below_the_enforced_floor():
    field = _build_additional_field("interval_minutes_field")
    with pytest.raises(ValidationError):
        field.clean("1")


def test_binary_bonus_interval_field_accepts_the_seeded_default():
    field = _build_additional_field("interval_minutes_field")
    assert field.clean("10") == 10


def test_matching_bonus_rate_and_interval_keys_reference_their_bounded_fields():
    """Task 14: same reasoning as the Binary Bonus fields above, applied
    from the start this time instead of as a reactive follow-up."""
    assert settings.CONSTANCE_CONFIG["MATCHING_BONUS_RATE"][2] == "percentage_field"
    assert (
        settings.CONSTANCE_CONFIG["MATCHING_BONUS_INTERVAL_DAYS"][2]
        == "interval_days_field"
    )


def test_matching_bonus_interval_field_rejects_below_the_enforced_floor():
    field = _build_additional_field("interval_days_field")
    with pytest.raises(ValidationError):
        field.clean("0")


def test_matching_bonus_interval_field_accepts_the_seeded_default():
    """CodeRabbit review, 2026-07-22: asserts the actual seeded default
    too, not just that the field accepts the literal "7" -- a changed
    default would otherwise leave this test green while no longer
    testing what its name claims."""
    assert settings.CONSTANCE_CONFIG["MATCHING_BONUS_INTERVAL_DAYS"][0] == 7
    field = _build_additional_field("interval_days_field")
    assert field.clean("7") == 7


def test_matching_bonus_interval_field_accepts_a_daily_cadence():
    """Unlike Binary Bonus's 5-minute floor, once-a-day (1) is a sane
    matching-bonus cadence, not a DoS-risk value to guard against."""
    field = _build_additional_field("interval_days_field")
    assert field.clean("1") == 1


def test_matching_bonus_depth_keys_reference_a_bounded_field():
    assert (
        settings.CONSTANCE_CONFIG["MATCHING_BONUS_DEPTH_BRONZE"][2]
        == "non_negative_depth_field"
    )
    assert (
        settings.CONSTANCE_CONFIG["MATCHING_BONUS_DEPTH_SILVER"][2]
        == "non_negative_depth_field"
    )


def test_matching_bonus_depth_field_rejects_negative():
    field = _build_additional_field("non_negative_depth_field")
    with pytest.raises(ValidationError):
        field.clean("-1")


def test_matching_bonus_depth_field_accepts_zero_meaning_unlimited():
    """0 is Silver's documented 'unlimited depth' sentinel, not an
    edge case to exclude."""
    field = _build_additional_field("non_negative_depth_field")
    assert field.clean("0") == 0


@pytest.mark.django_db
def test_min_withdrawal_amount_and_max_withdrawal_amount_seeded_values():
    assert config.MIN_WITHDRAWAL_AMOUNT == Decimal("100")
    assert config.MAX_WITHDRAWAL_AMOUNT == Decimal("10000")


@pytest.mark.django_db
def test_withdrawal_day_seeded_value():
    assert config.WITHDRAWAL_DAY == "friday"


def test_validate_withdrawal_amount_bounds_rejects_min_above_max():
    """CodeRabbit review, 2026-07-22: MIN_WITHDRAWAL_AMOUNT and
    MAX_WITHDRAWAL_AMOUNT were each validated independently (both just
    >= 0), so nothing stopped an admin from saving an inverted pair that
    would make every withdrawal request permanently unsatisfiable."""
    from apps.platform_settings.admin import validate_withdrawal_amount_bounds

    with pytest.raises(ValidationError):
        validate_withdrawal_amount_bounds(
            {
                "MIN_WITHDRAWAL_AMOUNT": Decimal("20000"),
                "MAX_WITHDRAWAL_AMOUNT": Decimal("10000"),
            }
        )


def test_validate_withdrawal_amount_bounds_accepts_min_below_max():
    from apps.platform_settings.admin import validate_withdrawal_amount_bounds

    validate_withdrawal_amount_bounds(
        {
            "MIN_WITHDRAWAL_AMOUNT": Decimal("100"),
            "MAX_WITHDRAWAL_AMOUNT": Decimal("10000"),
        }
    )


def test_validate_withdrawal_amount_bounds_accepts_equal_min_and_max():
    """A single fixed withdrawal amount (min == max) is a legitimate
    admin configuration, not an edge case to reject."""
    from apps.platform_settings.admin import validate_withdrawal_amount_bounds

    validate_withdrawal_amount_bounds(
        {
            "MIN_WITHDRAWAL_AMOUNT": Decimal("500"),
            "MAX_WITHDRAWAL_AMOUNT": Decimal("500"),
        }
    )
