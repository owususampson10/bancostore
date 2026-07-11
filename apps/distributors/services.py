from dataclasses import dataclass
from datetime import datetime, timedelta

from django.contrib.auth import authenticate
from django.contrib.auth.models import AbstractBaseUser
from django.utils import timezone

from constance import config

from .models import Distributor


@dataclass
class LoginAttempt:
    success: bool
    locked: bool = False
    locked_until: datetime | None = None
    needs_verification: bool = False
    user: AbstractBaseUser | None = None


def attempt_distributor_login(phone_number: str, password: str) -> LoginAttempt:
    """Phone+password login with wrong-password lockout, using
    django-constance thresholds (MAX_FAILED_LOGIN_ATTEMPTS,
    ACCOUNT_LOCKOUT_DURATION_MINUTES) rather than hardcoded numbers.

    An already-locked account only reports locked=True if the submitted
    password is actually correct. Checking lock state before the password
    would let anyone who knows/guesses a phone number confirm it's locked —
    and therefore a real, currently-targeted account — without ever
    needing to know the real password."""
    try:
        distributor = Distributor.objects.select_related("user").get(
            phone_number=phone_number
        )
    except Distributor.DoesNotExist:
        return LoginAttempt(success=False)

    now = timezone.now()
    if distributor.locked_until and distributor.locked_until > now:
        if distributor.user.check_password(password):
            return LoginAttempt(
                success=False, locked=True, locked_until=distributor.locked_until
            )
        return LoginAttempt(success=False)

    if distributor.locked_until and distributor.locked_until <= now:
        # Lock has expired — give them a fresh set of attempts.
        distributor.failed_login_attempts = 0
        distributor.locked_until = None

    user = authenticate(phone_number=phone_number, password=password)
    if user is None:
        distributor.failed_login_attempts += 1
        if distributor.failed_login_attempts >= config.MAX_FAILED_LOGIN_ATTEMPTS:
            distributor.locked_until = now + timedelta(
                minutes=config.ACCOUNT_LOCKOUT_DURATION_MINUTES
            )
        distributor.save(update_fields=["failed_login_attempts", "locked_until"])
        return LoginAttempt(
            success=False,
            locked=bool(distributor.locked_until),
            locked_until=distributor.locked_until,
        )

    distributor.failed_login_attempts = 0
    distributor.locked_until = None
    distributor.save(update_fields=["failed_login_attempts", "locked_until"])

    if not distributor.phone_verified:
        return LoginAttempt(success=False, needs_verification=True, user=user)

    return LoginAttempt(success=True, user=user)
