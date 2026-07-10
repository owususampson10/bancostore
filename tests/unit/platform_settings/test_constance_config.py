from decimal import Decimal

from django.conf import settings

import pytest
from constance import config


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
