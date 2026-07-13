import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import AbstractBaseUser, Group
from django.db import IntegrityError, transaction
from django.utils import timezone

from constance import config

from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

from .models import Distributor, PendingRegistration
from .paystack import PaystackError, verify_transaction

logger = logging.getLogger(__name__)

User = get_user_model()


class PendingRegistrationNotFound(Exception):
    pass


class PendingRegistrationAlreadyConsumed(Exception):
    pass


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


def snapshot_payment_reference(token) -> PendingRegistration:
    """Task 10b: atomically regenerates PendingRegistration.payment_reference
    and snapshots the current registration fee. Locked (matching this
    project's existing convention) so two concurrent requests for the same
    token -- a double-click, two open tabs -- can't overwrite each other's
    reference: the loser's reference would silently stop matching anything,
    and a customer who then pays via that now-orphaned checkout page would
    have no way to complete their registration."""

    def _attempt():
        with transaction.atomic():
            try:
                pending = select_for_update_nowait_if_supported(
                    PendingRegistration.objects.filter(token=token)
                ).get()
            except PendingRegistration.DoesNotExist:
                raise PendingRegistrationNotFound from None
            if pending.consumed_at is not None:
                raise PendingRegistrationAlreadyConsumed
            pending.fee_amount_pesewas = int(config.REGISTRATION_FEE * 100)
            pending.payment_reference = (
                f"reg-{pending.token.hex}-{uuid.uuid4().hex[:8]}"
            )
            pending.save(update_fields=["fee_amount_pesewas", "payment_reference"])
            return pending

    return retry_on_lock_contention(_attempt)


def consume_paid_registration(reference: str) -> None:
    """Task 10b: the single source of truth for turning a paid
    PendingRegistration into a real account. Called from BOTH the Paystack
    webhook and the callback-redirect view -- whichever arrives first
    completes it; both call sites, and repeat calls with the same
    reference, are always safe (idempotent). Design confirmed via
    doubt-driven-development 2026-07-13 -- see the
    project_paystack_registration_payment_design memory.

    Never trusts a caller's claims about payment status/amount/currency --
    always re-verifies server-side against Paystack's authoritative
    /transaction/verify endpoint before creating anything. Never raises:
    every failure path logs and returns, since a webhook handler crashing
    just means Paystack retries the same request for up to 72 hours.
    """

    def _attempt():
        with transaction.atomic():
            try:
                pending = select_for_update_nowait_if_supported(
                    PendingRegistration.objects.filter(payment_reference=reference)
                ).get()
            except PendingRegistration.DoesNotExist:
                logger.error(
                    "consume_paid_registration: no PendingRegistration found for "
                    "reference=%s -- a payment may have been confirmed with no "
                    "matching record (cleaned up, or a reference mismatch). "
                    "Needs manual investigation.",
                    reference,
                )
                return

            if pending.consumed_at is not None:
                return  # Already consumed -- idempotent no-op.

            try:
                verified = verify_transaction(reference)
            except PaystackError:
                logger.exception(
                    "consume_paid_registration: Paystack verify_transaction "
                    "failed for reference=%s",
                    reference,
                )
                return

            if verified.get("status") != "success":
                return
            if verified.get("currency") != "GHS":
                logger.warning(
                    "consume_paid_registration: unexpected currency %r for "
                    "reference=%s",
                    verified.get("currency"),
                    reference,
                )
                return
            if verified.get("amount") != pending.fee_amount_pesewas:
                logger.warning(
                    "consume_paid_registration: amount mismatch for "
                    "reference=%s (paid=%r, expected=%r)",
                    reference,
                    verified.get("amount"),
                    pending.fee_amount_pesewas,
                )
                return

            if Distributor.objects.filter(phone_number=pending.phone_number).exists():
                logger.error(
                    "consume_paid_registration: a Distributor with phone=%s "
                    "already exists -- refusing to create a duplicate for "
                    "reference=%s",
                    pending.phone_number,
                    reference,
                )
                return

            try:
                user = User(username=str(pending.phone_number), email=pending.email)
                # Already hashed in Task 10a via make_password() -- assigning
                # directly avoids double-hashing it through set_password().
                user.password = pending.password_hash
                user.save()
                distributor_group, _ = Group.objects.get_or_create(name="distributor")
                user.groups.add(distributor_group)
                Distributor.objects.create(
                    user=user,
                    phone_number=pending.phone_number,
                    full_name=pending.full_name,
                    address=pending.address,
                    area=pending.area,
                    landmark=pending.landmark,
                    sponsor=pending.sponsor,
                )
            except IntegrityError:
                logger.exception(
                    "consume_paid_registration: IntegrityError creating "
                    "account for reference=%s (likely a duplicate phone/"
                    "username race)",
                    reference,
                )
                return

            pending.consumed_at = timezone.now()
            pending.save(update_fields=["consumed_at"])

    retry_on_lock_contention(_attempt)
