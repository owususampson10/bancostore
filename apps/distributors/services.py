from dataclasses import dataclass
from datetime import datetime, timedelta

from django.contrib.auth import authenticate
from django.contrib.auth.models import AbstractBaseUser
from django.db import transaction
from django.utils import timezone

from constance import config

from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

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
    needing to know the real password.

    The counter read-modify-write (increment on failure / reset on
    success) is wrapped in select_for_update() + retry_on_lock_contention()
    — a real multi-threaded test reproduced a lost-update race here under
    concurrent failed logins before this fix (see
    tests/feature/distributors/test_distributor_auth.py::
    test_concurrent_failed_logins_do_not_lose_increments)."""
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

    # authenticate() does its own (slow, bcrypt-style) password check —
    # deliberately done outside any lock, both here and for the counter
    # update below, so a lock isn't held for the duration of a hash
    # comparison.
    user = authenticate(phone_number=phone_number, password=password)

    if user is None:

        def _record_failure():
            with transaction.atomic():
                locked = select_for_update_nowait_if_supported(Distributor.objects).get(
                    pk=distributor.pk
                )
                lock_now = timezone.now()
                if locked.locked_until and locked.locked_until <= lock_now:
                    # Lock had expired since the initial fetch — fresh
                    # attempts.
                    locked.failed_login_attempts = 0
                    locked.locked_until = None
                locked.failed_login_attempts += 1
                if locked.failed_login_attempts >= config.MAX_FAILED_LOGIN_ATTEMPTS:
                    locked.locked_until = lock_now + timedelta(
                        minutes=config.ACCOUNT_LOCKOUT_DURATION_MINUTES
                    )
                locked.save(update_fields=["failed_login_attempts", "locked_until"])
            return locked

        updated = retry_on_lock_contention(_record_failure)
        return LoginAttempt(
            success=False,
            locked=bool(updated.locked_until),
            locked_until=updated.locked_until,
        )

    def _record_success():
        with transaction.atomic():
            locked = select_for_update_nowait_if_supported(Distributor.objects).get(
                pk=distributor.pk
            )
            locked.failed_login_attempts = 0
            locked.locked_until = None
            locked.save(update_fields=["failed_login_attempts", "locked_until"])
        return locked

    retry_on_lock_contention(_record_success)

    if not distributor.phone_verified:
        return LoginAttempt(success=False, needs_verification=True, user=user)

    return LoginAttempt(success=True, user=user)
