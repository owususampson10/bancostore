import logging

from django.core.exceptions import ValidationError

from constance import config

logger = logging.getLogger(__name__)

# Security-review finding (Task 30a): an admin fat-fingering
# MIN_PASSWORD_LENGTH to 0 (or negative) in the constance UI must not be
# able to fully disable minimum-length enforcement platform-wide.
ABSOLUTE_MIN_PASSWORD_LENGTH = 6

# Read config.* inside validate()/get_help_text(), never in __init__ or
# cached on self -- Django caches password validator instances for the
# whole process lifetime (get_default_password_validators), so a value
# read once at construction time would silently freeze it, defeating the
# whole point of an admin-editable, no-restart-needed setting.


def _min_password_length():
    try:
        configured = config.MIN_PASSWORD_LENGTH
    except Exception:
        # constance's cache backend (Redis) is unreachable -- degrade to
        # the safe hardcoded floor rather than a hard failure on every
        # password-set path in the app during an outage. Fail-CLOSED:
        # this guards an actual security control (contrast with
        # context_processors.py::google_login_flags's fail-OPEN default,
        # which only hides/shows a cosmetic button).
        logger.warning(
            "ConfigurableMinimumLengthValidator: could not read "
            "MIN_PASSWORD_LENGTH from constance, falling back to %d",
            ABSOLUTE_MIN_PASSWORD_LENGTH,
        )
        return ABSOLUTE_MIN_PASSWORD_LENGTH
    return max(configured, ABSOLUTE_MIN_PASSWORD_LENGTH)


class ConfigurableMinimumLengthValidator:
    def validate(self, password, user=None):
        min_length = _min_password_length()
        if len(password) < min_length:
            raise ValidationError(
                f"This password must contain at least {min_length} characters.",
                code="password_too_short",
            )

    def get_help_text(self):
        return (
            f"Your password must contain at least {_min_password_length()} characters."
        )


def _password_complexity_enabled():
    try:
        return config.PASSWORD_COMPLEXITY_ENABLED
    except Exception:
        # Fail-CLOSED (defaults to enabled, the stricter behavior) --
        # same reasoning as _min_password_length above: this guards an
        # actual security control, unlike google_login_flags's
        # fail-OPEN default for a cosmetic button.
        logger.warning(
            "ConfigurablePasswordComplexityValidator: could not read "
            "PASSWORD_COMPLEXITY_ENABLED from constance, falling back to enabled"
        )
        return True


class ConfigurablePasswordComplexityValidator:
    def validate(self, password, user=None):
        if not _password_complexity_enabled():
            return
        has_upper = any(c.isupper() for c in password)
        has_digit = any(c.isdigit() for c in password)
        if not has_upper or not has_digit:
            raise ValidationError(
                "This password must contain at least one uppercase letter "
                "and one number.",
                code="password_not_complex_enough",
            )

    def get_help_text(self):
        if not _password_complexity_enabled():
            return ""
        return (
            "Your password must contain at least one uppercase letter and "
            "one number."
        )
