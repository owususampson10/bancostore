from unittest.mock import patch

from django.core.exceptions import ValidationError

import pytest
from constance import config

from apps.accounts.validators import (
    ABSOLUTE_MIN_PASSWORD_LENGTH,
    ConfigurableMinimumLengthValidator,
    ConfigurablePasswordComplexityValidator,
)


@pytest.mark.django_db
def test_rejects_a_password_shorter_than_the_live_config_value():
    config.MIN_PASSWORD_LENGTH = 12

    with pytest.raises(ValidationError):
        ConfigurableMinimumLengthValidator().validate("short1A")


@pytest.mark.django_db
def test_accepts_a_password_at_exactly_the_configured_minimum():
    config.MIN_PASSWORD_LENGTH = 10

    ConfigurableMinimumLengthValidator().validate("a" * 10)


@pytest.mark.django_db
def test_a_config_value_below_the_absolute_floor_is_clamped_up():
    """Security-review finding: an admin fat-fingering MIN_PASSWORD_LENGTH
    to 0 (or negative) in the constance UI must not fully disable the
    control platform-wide."""
    config.MIN_PASSWORD_LENGTH = 0

    with pytest.raises(ValidationError):
        ConfigurableMinimumLengthValidator().validate(
            "a" * (ABSOLUTE_MIN_PASSWORD_LENGTH - 1)
        )

    ConfigurableMinimumLengthValidator().validate("a" * ABSOLUTE_MIN_PASSWORD_LENGTH)


@pytest.mark.django_db
def test_help_text_reflects_the_live_config_value():
    config.MIN_PASSWORD_LENGTH = 14

    assert "14" in ConfigurableMinimumLengthValidator().get_help_text()


class _ExplodingConfig:
    """Stands in for constance's config proxy when its cache backend
    (Redis) is unreachable -- any attribute access raises, matching
    django_redis's default (non-IGNORE_EXCEPTIONS) failure mode."""

    def __getattr__(self, name):
        raise ConnectionError("redis down")


@pytest.mark.django_db
def test_falls_back_to_the_absolute_floor_if_constance_is_unreachable():
    """Security-review finding: a Redis outage (constance's cache
    backend) must degrade password validation to the safe hardcoded
    floor, not crash every registration/password-reset/password-change
    form in the app with an unhandled exception."""
    with patch("apps.accounts.validators.config", new=_ExplodingConfig()):
        ConfigurableMinimumLengthValidator().validate(
            "a" * ABSOLUTE_MIN_PASSWORD_LENGTH
        )

        with pytest.raises(ValidationError):
            ConfigurableMinimumLengthValidator().validate(
                "a" * (ABSOLUTE_MIN_PASSWORD_LENGTH - 1)
            )


@pytest.mark.django_db
def test_complexity_validator_falls_back_to_enabled_if_constance_is_unreachable():
    with patch("apps.accounts.validators.config", new=_ExplodingConfig()):
        with pytest.raises(ValidationError):
            ConfigurablePasswordComplexityValidator().validate(
                "all lowercase no digits"
            )


@pytest.mark.django_db
def test_complexity_validator_rejects_a_password_with_no_uppercase_letter():
    config.PASSWORD_COMPLEXITY_ENABLED = True

    with pytest.raises(ValidationError):
        ConfigurablePasswordComplexityValidator().validate("lowercase1")


@pytest.mark.django_db
def test_complexity_validator_rejects_a_password_with_no_digit():
    config.PASSWORD_COMPLEXITY_ENABLED = True

    with pytest.raises(ValidationError):
        ConfigurablePasswordComplexityValidator().validate("NoDigitsHere")


@pytest.mark.django_db
def test_complexity_validator_accepts_a_password_with_both_when_enabled():
    config.PASSWORD_COMPLEXITY_ENABLED = True

    ConfigurablePasswordComplexityValidator().validate("Valid1Password")


@pytest.mark.django_db
def test_complexity_validator_accepts_anything_when_disabled():
    config.PASSWORD_COMPLEXITY_ENABLED = False

    ConfigurablePasswordComplexityValidator().validate("all lowercase no digits")


@pytest.mark.django_db
def test_complexity_help_text_is_empty_when_disabled():
    config.PASSWORD_COMPLEXITY_ENABLED = False

    assert ConfigurablePasswordComplexityValidator().get_help_text() == ""


@pytest.mark.django_db
def test_complexity_help_text_is_present_when_enabled():
    config.PASSWORD_COMPLEXITY_ENABLED = True

    assert ConfigurablePasswordComplexityValidator().get_help_text() != ""
