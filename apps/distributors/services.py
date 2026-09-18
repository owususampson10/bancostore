import ipaddress
import logging
import re
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from urllib.parse import urlparse

from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.models import AbstractBaseUser, Group
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

import requests
from constance import config

from apps.binary_tree.services import AlreadyPlacedError, BinaryTree
from apps.commissions.services import calculate_direct_referral_bonus
from apps.notifications.models import Notification, NotificationTemplate
from apps.notifications.otp import OtpDeliveryFailed, generate_otp
from apps.notifications.rendering import render_or_default
from apps.notifications.services import send_notification
from apps.notifications.sms import send_sms
from apps.pv_ledger.services import record_personal_pv, record_purchase_pv
from apps.wallet.models import WalletTransaction
from apps.wallet.services import credit as credit_wallet
from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)
from bancostore.media import resize_and_convert_to_webp

from .didit import DiditError, get_session_decision
from .models import (
    DiditVerification,
    Distributor,
    IrIdSequence,
    PaymentIssue,
    PendingRegistration,
    RegistrationCheckout,
    StarterPackCheckout,
)
from .payment_issues import record_payment_issue
from .payment_outcomes import PaymentOutcome
from .paystack import PaystackError, PaystackNotFoundError, verify_transaction

logger = logging.getLogger(__name__)

User = get_user_model()


class PendingRegistrationNotFound(Exception):
    pass


class IrIdSequenceExhausted(Exception):
    pass


class PendingRegistrationAlreadyConsumed(Exception):
    pass


class StarterPackAlreadyConfirmed(Exception):
    pass


class MembershipCancelled(Exception):
    """Task 19 (doubt-driven-development finding, pre-implementation
    review of the cooling-off refund): raised by
    snapshot_starter_pack_choice for a distributor whose
    cooling_off_cancelled_at is set. Distinct from
    StarterPackAlreadyConfirmed -- without this guard, an admin
    reactivating a cooling-off-cancelled account (apps/admin_portal's
    existing, unrelated suspend/reactivate toggle on user.is_active,
    which has no knowledge of cooling_off_cancelled_at) would let them
    re-select and re-pay for a starter pack. BinaryTreeEdge placement was
    never removed (ADR-0007), so consume_paid_starter_pack's
    AlreadyPlacedError-catch path would then credit PV and the sponsor's
    direct referral bonus a second time for a position that was already
    paid for and already refunded once."""

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
            pending.payment_initialized_at = timezone.now()
            # Remembered per reference, so a payment made on an earlier tab
            # is checked against the fee IT was issued at, not a fee an admin
            # has since changed (adversarial security review).
            RegistrationCheckout.objects.create(
                pending_registration=pending,
                reference=pending.payment_reference,
                fee_amount_pesewas=pending.fee_amount_pesewas,
            )
            pending.save(
                update_fields=[
                    "fee_amount_pesewas",
                    "payment_reference",
                    "payment_initialized_at",
                ]
            )
            return pending

    return retry_on_lock_contention(_attempt)


# Task 67. The exact shape snapshot_payment_reference issues. Only a reference
# of this shape is worth looking up by token or verifying with Paystack when
# no row matches it -- the callback that passes references in is public.
_REGISTRATION_REFERENCE = re.compile(r"^reg-([0-9a-f]{32})-[0-9a-f]{8}$")


def consume_paid_registration(reference: str) -> PaymentOutcome:
    """Task 10b: the single source of truth for turning a paid
    PendingRegistration into a real account. Called from the Paystack
    webhook, the callback-redirect view, pending-registration cleanup and
    the daily payment reconciliation -- whichever arrives first completes
    it; repeat calls with the same reference are always safe (idempotent).
    Design confirmed via doubt-driven-development 2026-07-13 -- see the
    project_paystack_registration_payment_design memory.

    Never trusts a caller's claims about payment status/amount/currency --
    always re-verifies server-side against Paystack's authoritative
    /transaction/verify endpoint before creating anything. Never raises
    for a payment problem: a webhook handler crashing just means Paystack
    retries the same request.

    Task 67 (found 2026-09-16, when a fee paid on a checkout page left open
    past cleanup created nothing and left only a log line): a payment
    Paystack confirms as successful now ALWAYS ends in an account or a
    PaymentIssue the admin is told about. Specifically:
    - a reference from an older checkout tab (every visit to the payment
      step issues a new one) is resolved through the token it carries;
    - a second successful payment for an already-created account is an
      issue, but a replay of the reference that created it is not
      (consumed_reference tells them apart);
    - an amount/currency mismatch, an existing distributor on the phone, or
      an account-creation error on a PAID reference is an issue, not just a
      warning in the log.
    Issues are recorded after the locked transaction returns, never inside
    it, so a rollback can't take the record with it."""

    def _attempt():
        with transaction.atomic():
            match = _REGISTRATION_REFERENCE.match(reference)
            pending = select_for_update_nowait_if_supported(
                PendingRegistration.objects.filter(payment_reference=reference)
            ).first()
            if pending is None and match:
                pending = select_for_update_nowait_if_supported(
                    PendingRegistration.objects.filter(token=uuid.UUID(match.group(1)))
                ).first()

            if pending is None:
                if not match:
                    logger.error(
                        "consume_paid_registration: no PendingRegistration found "
                        "for reference=%s, and it is not a registration "
                        "reference this site issues -- not checked with Paystack.",
                        reference,
                    )
                    return _Result(PaymentOutcome.UNKNOWN_REFERENCE)
                result = _verify_paid(reference)
                if result.outcome is not None:
                    return result
                return _issue(
                    PaymentIssue.Kind.REGISTRATION_UNMATCHED,
                    result.verified,
                    detail="No pending registration was found for this payment "
                    "(it may have been cleaned up before the payment arrived).",
                )

            if pending.consumed_at is not None:
                if pending.consumed_reference == reference:
                    return _Result(PaymentOutcome.ALREADY_APPLIED)
                result = _verify_paid(reference)
                if result.outcome is not None:
                    return result
                return _issue(
                    PaymentIssue.Kind.REGISTRATION_DUPLICATE,
                    result.verified,
                    pending,
                    detail=f"The account was already created by payment "
                    f"{pending.consumed_reference}; this is a second payment.",
                )

            result = _verify_paid(reference)
            if result.outcome is not None:
                return result
            verified = result.verified

            if verified.get("currency") != "GHS":
                logger.warning(
                    "consume_paid_registration: unexpected currency %r for "
                    "reference=%s",
                    verified.get("currency"),
                    reference,
                )
                return _issue(
                    PaymentIssue.Kind.REGISTRATION_NOT_CREATED,
                    verified,
                    pending,
                    detail=f"Paid in {verified.get('currency')!r}, not GHS.",
                )
            # The fee this very checkout was issued at, not the latest one.
            checkout = RegistrationCheckout.objects.filter(reference=reference).first()
            expected_pesewas = (
                checkout.fee_amount_pesewas
                if checkout is not None
                else pending.fee_amount_pesewas
            )
            if verified.get("amount") != expected_pesewas:
                logger.warning(
                    "consume_paid_registration: amount mismatch for "
                    "reference=%s (paid=%r, expected=%r)",
                    reference,
                    verified.get("amount"),
                    expected_pesewas,
                )
                return _issue(
                    PaymentIssue.Kind.REGISTRATION_NOT_CREATED,
                    verified,
                    pending,
                    detail=f"Paid {verified.get('amount')!r} pesewas, expected "
                    f"{expected_pesewas!r}.",
                )

            if Distributor.objects.filter(phone_number=pending.phone_number).exists():
                logger.error(
                    "consume_paid_registration: a Distributor with phone=%s "
                    "already exists -- refusing to create a duplicate for "
                    "reference=%s",
                    pending.phone_number,
                    reference,
                )
                return _issue(
                    PaymentIssue.Kind.REGISTRATION_NOT_CREATED,
                    verified,
                    pending,
                    detail="A distributor with this phone number already exists.",
                )

            try:
                # Task 67: its own savepoint. A failed save inside the outer
                # atomic() would otherwise mark the whole transaction for
                # rollback, and the caller would fail on its next query.
                with transaction.atomic():
                    user = User(username=str(pending.phone_number), email=pending.email)
                    # Already hashed in Task 10a via make_password() --
                    # assigning directly avoids double-hashing it through
                    # set_password().
                    user.password = pending.password_hash
                    user.save()
                    distributor_group, _ = Group.objects.get_or_create(
                        name="distributor"
                    )
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
                return _issue(
                    PaymentIssue.Kind.REGISTRATION_NOT_CREATED,
                    verified,
                    pending,
                    detail="The account could not be created (the phone number "
                    "is already in use).",
                )

            pending.consumed_at = timezone.now()
            pending.consumed_reference = reference
            pending.save(update_fields=["consumed_at", "consumed_reference"])
            return _Result(PaymentOutcome.APPLIED)

    def _verify_paid(ref):
        try:
            verified = verify_transaction(ref)
        except PaystackNotFoundError:
            return _Result(PaymentOutcome.NOT_PAID)
        except PaystackError:
            logger.exception(
                "consume_paid_registration: Paystack verify_transaction "
                "failed for reference=%s",
                ref,
            )
            return _Result(PaymentOutcome.VERIFY_FAILED)
        if verified.get("status") != "success":
            return _Result(PaymentOutcome.NOT_PAID)
        return _Result(None, verified=verified)

    def _issue(kind, verified, pending=None, detail=""):
        return _Result(
            PaymentOutcome.ISSUE_RECORDED,
            issue_kind=kind,
            verified=verified,
            pending=pending,
            detail=detail,
        )

    result = retry_on_lock_contention(_attempt)
    if result.issue_kind is not None:
        issue = record_payment_issue(
            reference,
            result.issue_kind,
            verified=result.verified,
            pending=result.pending,
            detail=result.detail,
        )
        if issue is None:
            # Code review (Task 67): nothing durable was written, so this
            # must not read as handled -- VERIFY_FAILED makes the webhook
            # ask Paystack to retry and makes cleanup keep the row that
            # still holds the payer's details.
            return PaymentOutcome.VERIFY_FAILED
    return result.outcome


@dataclass
class _Result:
    """What one locked consume attempt concluded, carried back out so the
    PaymentIssue is recorded after the transaction commits rather than
    inside it (Task 67: a write inside the block would be rolled back with
    it, losing the only record of the payer)."""

    outcome: PaymentOutcome | None
    issue_kind: str | None = None
    verified: dict | None = None
    pending: PendingRegistration | None = None
    distributor: Distributor | None = None
    detail: str = ""


class PendingRegistrationResolution(str, Enum):
    DELETED = "deleted"
    CONSUMED = "consumed"
    KEPT = "kept"


# Task 67. Paystack statuses that mean this reference has not been paid.
# "abandoned" is only "not yet": the 2026-09-16 payment went from abandoned
# to success after 1h43m, which is why a row is never judged before it has
# been idle for the caller's whole window.
_NOT_PAID_STATUSES = frozenset({"abandoned", "failed", "reversed"})


def resolve_unconsumed_pending_registration(
    pk, *, idle_for, max_age, delete_unconfirmed
) -> PendingRegistrationResolution:
    """Task 67. Decides whether an unconsumed PendingRegistration can go.
    Shared by the cleanup task (idle 72h / max 30 days) and by a new
    registration taking over an abandoned one's phone number (idle 1h /
    max 24h).

    Before this, cleanup deleted anything over an hour old without asking
    Paystack, and on 2026-09-16 that deleted a registration whose checkout
    was paid 43 minutes later -- money taken, no account, one log line.

    A row is only looked at once its last checkout has been idle for
    `idle_for`, or it is older than `max_age` (which stops a checkout being
    reopened hourly to hold a phone number forever). It is then deleted only
    when:
    - it never reached the payment step;
    - Paystack says the reference was never seen, or was not paid;
    - it was paid, and consume_paid_registration -- in THIS call -- turned
      the payment into a recorded PaymentIssue (the issue keeps the payer's
      details);
    - it is older than `max_age` and Paystack answers with anything but
      "success" (e.g. a mobile-money charge started and never approved) --
      otherwise a stranger could hold a number by starting a charge and
      leaving it. A payment that still lands later reaches the webhook or
      reconciliation as an unmatched PaymentIssue, so no money is lost;
    - `delete_unconfirmed` is set (cleanup), it is older than `max_age`, and
      Paystack could not be reached at all: it becomes a PaymentIssue first,
      since whether it was paid is genuinely unknown.
    Anything else is kept for a later run. The delete re-checks the row
    under a lock, so a checkout reopened or a payment consumed while
    Paystack was being asked stops it."""
    pending = PendingRegistration.objects.filter(pk=pk).first()
    if pending is None:
        return PendingRegistrationResolution.DELETED
    if pending.consumed_at is not None:
        return PendingRegistrationResolution.CONSUMED

    now = timezone.now()
    last_activity = pending.payment_initialized_at or pending.created_at
    past_max_age = pending.created_at <= now - max_age
    if not past_max_age and last_activity > now - idle_for:
        return PendingRegistrationResolution.KEPT

    reference = pending.payment_reference
    if reference is None:
        return _delete_pending_if_unchanged(pending)

    try:
        verified = verify_transaction(reference)
    except PaystackNotFoundError:
        return _delete_pending_if_unchanged(pending)
    except PaystackError:
        logger.warning(
            "resolve_unconsumed_pending_registration: could not verify "
            "reference=%s -- keeping it for now.",
            reference,
            exc_info=True,
        )
        verified = None
    else:
        status = verified.get("status")
        if status in _NOT_PAID_STATUSES:
            return _delete_pending_if_unchanged(pending)
        if status == "success":
            outcome = consume_paid_registration(reference)
            if outcome in (
                PaymentOutcome.APPLIED,
                PaymentOutcome.ALREADY_APPLIED,
            ):
                return PendingRegistrationResolution.CONSUMED
            if outcome is PaymentOutcome.ISSUE_RECORDED:
                return _delete_pending_if_unchanged(pending)
            return PendingRegistrationResolution.KEPT
        if past_max_age:
            return _delete_pending_if_unchanged(pending)
        return PendingRegistrationResolution.KEPT

    if delete_unconfirmed and past_max_age:
        return _delete_pending_if_unchanged(
            pending,
            issue_detail=(
                "Paystack could not be reached to check this registration's "
                "payment before it reached the maximum age, so its details "
                "were removed."
            ),
        )
    return PendingRegistrationResolution.KEPT


def _delete_pending_if_unchanged(pending, *, issue_detail=None):
    """Deletes the row only if, under a lock, it is still unconsumed and has
    the same checkout it had when Paystack was asked. When `issue_detail` is
    given, the PaymentIssue is recorded in the same transaction as the
    delete, so one never happens without the other."""

    def _attempt():
        with transaction.atomic():
            current = select_for_update_nowait_if_supported(
                PendingRegistration.objects.filter(pk=pending.pk)
            ).first()
            if current is None:
                return PendingRegistrationResolution.DELETED
            if current.consumed_at is not None:
                return PendingRegistrationResolution.CONSUMED
            if (
                current.payment_reference != pending.payment_reference
                or current.payment_initialized_at != pending.payment_initialized_at
            ):
                return PendingRegistrationResolution.KEPT
            if issue_detail is not None:
                issue = record_payment_issue(
                    current.payment_reference,
                    PaymentIssue.Kind.REGISTRATION_UNCONFIRMED,
                    pending=current,
                    detail=issue_detail,
                )
                if issue is None:
                    return PendingRegistrationResolution.KEPT
            current.delete()
            return PendingRegistrationResolution.DELETED

    return retry_on_lock_contention(_attempt)


def snapshot_starter_pack_choice(distributor_pk, choice: str) -> Distributor:
    """Task 10c: atomically pins the price/PV/rank for the chosen starter
    pack (from django-constance, at selection time -- never re-derived
    live at confirmation time) and generates a fresh Paystack reference.
    Locked for the same reason as snapshot_payment_reference: two
    concurrent requests for the same distributor must not overwrite each
    other's reference. The constance read lives inside the retried
    transaction too -- a cold constance cache falls back to a real DB
    query (constance.backends.database), which under SQLite's single
    writer lock can itself raise "database is locked" under concurrent
    threads if left unprotected, the same class of issue fixed in Task
    10a's PvLedger creation."""

    def _attempt():
        with transaction.atomic():
            if choice == "A":
                price, pv, rank = (
                    config.STARTER_PACK_A_PRICE,
                    config.STARTER_PACK_A_PV,
                    config.STARTER_PACK_A_RANK,
                )
            else:
                price, pv, rank = (
                    config.STARTER_PACK_B_PRICE,
                    config.STARTER_PACK_B_PV,
                    config.STARTER_PACK_B_RANK,
                )
            distributor = select_for_update_nowait_if_supported(
                Distributor.objects.filter(pk=distributor_pk)
            ).get()
            if distributor.starter_pack_confirmed_at is not None:
                raise StarterPackAlreadyConfirmed
            if distributor.cooling_off_cancelled_at is not None:
                raise MembershipCancelled
            distributor.starter_pack_choice = choice
            distributor.starter_pack_price_pesewas = int(price * 100)
            distributor.starter_pack_pv = pv
            distributor.starter_pack_rank = rank
            distributor.starter_pack_payment_reference = (
                f"pack-{distributor.pk}-{uuid.uuid4().hex[:8]}"
            )
            # Task 68b: remembered, not just overwritten. A distributor who
            # goes back and re-picks leaves a live, payable checkout behind,
            # and this is the only record that it was ever ours -- with the
            # price as it stood then, so a later price change cannot turn a
            # correct payment into a rejected one.
            StarterPackCheckout.objects.create(
                distributor=distributor,
                reference=distributor.starter_pack_payment_reference,
                amount_pesewas=distributor.starter_pack_price_pesewas,
                choice=choice,
                pv=pv,
                rank=rank,
            )
            distributor.save(
                update_fields=[
                    "starter_pack_choice",
                    "starter_pack_price_pesewas",
                    "starter_pack_pv",
                    "starter_pack_rank",
                    "starter_pack_payment_reference",
                ]
            )
            return distributor

    return retry_on_lock_contention(_attempt)


def consume_paid_starter_pack(reference: str) -> PaymentOutcome:
    """Task 10c/10d: mirrors consume_paid_registration's idempotent,
    server-verified pattern. Sets rank from the snapshotted
    starter_pack_rank (never re-derived from constance at confirmation
    time) once Paystack confirms the exact pinned amount was paid in GHS.

    Task 10d: this is also the distributor's first-ever binary tree
    placement (place_distributor is never called anywhere else) and the
    write-time PV credit up the ancestor chain -- both happen inside the
    same locked, idempotency-checked block as the rank change, so a
    webhook/callback race can't double-place or double-credit PV. Leg
    choice is always auto-balance (leg=None): Task 10a's registration
    form has no field for a sponsor to pick an explicit leg, so the
    "sponsor picks, or auto-balance falls back" design only exercises the
    fallback path today (confirmed with the user 2026-07-13).

    Task 68c/68d: returns a PaymentOutcome instead of None, so the webhook
    can ask Paystack to retry a failed verify rather than acknowledging its
    one push; and a payment Paystack confirms that cannot be applied now
    records a PaymentIssue here, at the failure site, instead of only being
    noticed by the next day's reconciliation. Issues are recorded after the
    locked transaction returns, never inside it."""

    def _attempt():
        with transaction.atomic():
            # Task 68b: the checkout this reference belongs to, whether or
            # not it is still the distributor's current one.
            checkout = StarterPackCheckout.objects.filter(reference=reference).first()
            distributor_filter = (
                Distributor.objects.filter(pk=checkout.distributor_id)
                if checkout is not None
                else Distributor.objects.filter(
                    starter_pack_payment_reference=reference
                )
            )
            try:
                distributor = select_for_update_nowait_if_supported(
                    distributor_filter
                ).get()
            except Distributor.DoesNotExist:
                logger.error(
                    "consume_paid_starter_pack: no Distributor found for "
                    "reference=%s -- a payment may have been confirmed with "
                    "no matching record. Needs manual investigation.",
                    reference,
                )
                # An orphaned checkout from an earlier pack selection is the
                # likeliest cause, and the money is real if it was paid.
                result = _verify_pack_paid(reference)
                if result.outcome is not None:
                    return result
                return _pack_issue(
                    result.verified,
                    detail="No distributor is waiting on this starter-pack "
                    "payment (an earlier pack selection's checkout, or a "
                    "record since removed).",
                )

            if distributor.starter_pack_confirmed_at is not None:
                if checkout is None or checkout.consumed_at is not None:
                    # A replay of the payment that applied the pack.
                    return _Result(PaymentOutcome.ALREADY_APPLIED)
                # Task 68b: a SECOND checkout paid for as well -- the pack is
                # already theirs, so this money needs giving back.
                result = _verify_pack_paid(reference)
                if result.outcome is not None:
                    return result
                return _pack_issue(
                    result.verified,
                    distributor,
                    detail="This distributor's starter pack was already paid "
                    "for and applied by an earlier checkout, so this is a "
                    "second payment.",
                )

            # Task 19 (security-and-hardening review, post-implementation):
            # cancel_membership_and_refund clears starter_pack_confirmed_at
            # back to None, so the idempotency check above no longer
            # protects against a REPLAYED webhook/callback for the same old
            # reference after cancellation -- a genuinely successful
            # Paystack transaction stays verifiable as "success" forever.
            # starter_pack_price_pesewas is also cleared, so the amount
            # check below would incidentally catch this too -- but relying
            # on that as the only defense is fragile (a future change to
            # what gets cleared could silently reopen it). This is the
            # same AlreadyPlacedError double-credit path MembershipCancelled
            # was added to close for the re-selection route -- this guard
            # closes it for the payment-replay route.
            if distributor.cooling_off_cancelled_at is not None:
                logger.warning(
                    "consume_paid_starter_pack: distributor_id=%s "
                    "reference=%s -- membership was already cancelled via "
                    "cooling-off, ignoring this payment confirmation.",
                    distributor.pk,
                    reference,
                )
                result = _verify_pack_paid(reference)
                if result.outcome is not None:
                    return result
                return _pack_issue(
                    result.verified,
                    distributor,
                    detail="This membership was already cancelled under the "
                    "cooling-off rule, so the pack was not applied.",
                )

            result = _verify_pack_paid(reference)
            if result.outcome is not None:
                return result
            verified = result.verified

            if verified.get("currency") != "GHS":
                logger.warning(
                    "consume_paid_starter_pack: unexpected currency %r for "
                    "reference=%s",
                    verified.get("currency"),
                    reference,
                )
                return _pack_issue(
                    verified,
                    distributor,
                    detail=f"Paid in {verified.get('currency')!r}, not GHS.",
                )
            # The price this very checkout was opened at, not whatever the
            # distributor's current selection says (Task 68b).
            expected_pesewas = (
                checkout.amount_pesewas
                if checkout is not None
                else distributor.starter_pack_price_pesewas
            )
            if verified.get("amount") != expected_pesewas:
                logger.warning(
                    "consume_paid_starter_pack: amount mismatch for "
                    "reference=%s (paid=%r, expected=%r)",
                    reference,
                    verified.get("amount"),
                    expected_pesewas,
                )
                return _pack_issue(
                    verified,
                    distributor,
                    detail=f"Paid {verified.get('amount')!r} pesewas, expected "
                    f"{expected_pesewas!r}.",
                )

            try:
                BinaryTree.place_distributor(distributor.sponsor, distributor, leg=None)
            except AlreadyPlacedError:
                logger.warning(
                    "consume_paid_starter_pack: distributor=%s was already "
                    "placed in the binary tree before starter-pack "
                    "confirmation -- unexpected given the current call "
                    "graph (place_distributor has no other caller), but "
                    "proceeding safely; PV is still credited below.",
                    distributor.pk,
                )

            # Task 19 (doubt-driven-development finding): pinned once and
            # reused for the PV credit AND starter_pack_confirmed_at --
            # three independent timezone.now() calls here previously
            # (record_purchase_pv's own default, record_personal_pv's own
            # default, and this assignment) could in principle straddle a
            # UTC-midnight boundary and land in different calendar dates,
            # exactly the class of bug Task 18b's own `today` parameter on
            # confirm_order_payment was already built to avoid. Task 19's
            # cooling-off refund reversal uses starter_pack_confirmed_at
            # .date() as its purchase_date anchor, so it must always match
            # the date the credit itself actually used.
            now = timezone.now()
            today = now.date()

            # Task 68b (agent code review): the pack THIS checkout was for,
            # not whatever the distributor has since re-selected. Applying
            # the current selection to an older checkout's payment would
            # hand out a dearer pack for a cheaper pack's money.
            pack_pv = (
                checkout.pv if checkout is not None else distributor.starter_pack_pv
            )
            pack_rank = (
                checkout.rank if checkout is not None else distributor.starter_pack_rank
            )

            record_purchase_pv(distributor, pack_pv, today=today)
            # Task 13a: record_purchase_pv only credits ANCESTORS' legs --
            # nothing previously credited the purchasing distributor's own
            # personal PV, needed for Binary/Matching Bonus eligibility.
            record_personal_pv(distributor, pack_pv, today=today)

            if distributor.sponsor_id:
                _credit_direct_referral_bonus(distributor, reference, pack_pv)

            distributor.rank = pack_rank
            # Task 68 (third security review): the pack that was PAID FOR is
            # written back, not left as whatever was last selected. Every
            # later reader -- the cooling-off refund amount, the ancestor PV
            # reversal -- uses these fields, so leaving them on the newer
            # selection refunded a GHS 2,000 pack for a GHS 500 payment and
            # stripped PV from uplines that was never credited to them.
            distributor.starter_pack_pv = pack_pv
            distributor.starter_pack_rank = pack_rank
            if checkout is not None:
                distributor.starter_pack_choice = checkout.choice
                distributor.starter_pack_price_pesewas = checkout.amount_pesewas
            distributor.starter_pack_confirmed_at = now
            distributor.save(
                update_fields=[
                    "rank",
                    "starter_pack_choice",
                    "starter_pack_price_pesewas",
                    "starter_pack_pv",
                    "starter_pack_rank",
                    "starter_pack_confirmed_at",
                ]
            )
            if checkout is not None:
                checkout.consumed_at = now
                checkout.save(update_fields=["consumed_at"])
            return _Result(PaymentOutcome.APPLIED)

    def _verify_pack_paid(ref):
        try:
            verified = verify_transaction(ref)
        except PaystackNotFoundError:
            return _Result(PaymentOutcome.NOT_PAID)
        except PaystackError:
            logger.exception(
                "consume_paid_starter_pack: Paystack verify_transaction "
                "failed for reference=%s",
                ref,
            )
            return _Result(PaymentOutcome.VERIFY_FAILED)
        if verified.get("status") != "success":
            return _Result(PaymentOutcome.NOT_PAID)
        return _Result(None, verified=verified)

    def _pack_issue(verified, distributor=None, *, detail=""):
        return _Result(
            PaymentOutcome.ISSUE_RECORDED,
            issue_kind=PaymentIssue.Kind.STARTER_PACK_NOT_APPLIED,
            verified=verified,
            distributor=distributor,
            detail=detail,
        )

    result = retry_on_lock_contention(_attempt)
    if result.issue_kind is not None:
        issue = record_payment_issue(
            reference,
            result.issue_kind,
            verified=result.verified,
            distributor=result.distributor,
            detail=result.detail,
        )
        if issue is None:
            # Nothing durable was written, so this must not read as
            # resolved: the webhook asks Paystack to send it again.
            return PaymentOutcome.VERIFY_FAILED
    return result.outcome


def _credit_direct_referral_bonus(distributor, reference: str, pack_pv=None) -> None:
    """Task 12b/12c: instant credit to distributor.sponsor, plus an SMS
    notification. Called from inside consume_paid_starter_pack's own
    locked/idempotent block, so a webhook/callback race can't
    double-credit -- this function does no locking of its own. Only
    called when distributor.sponsor_id is set; root-of-tree distributors
    don't generate this bonus."""
    # Task 68b: the PV of the pack actually paid for, which is not always
    # the distributor's current selection (see consume_paid_starter_pack).
    if pack_pv is None:
        pack_pv = distributor.starter_pack_pv
    bonus = calculate_direct_referral_bonus(pack_pv)
    credit_wallet(
        distributor.sponsor,
        bonus,
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference=reference,
    )
    logger.info(
        "consume_paid_starter_pack: direct referral bonus GHS %s credited to "
        "sponsor_id=%s for referred_distributor_id=%s reference=%s",
        bonus,
        distributor.sponsor_id,
        distributor.pk,
        reference,
    )
    # The bonus has already landed -- an SMS provider outage must never
    # roll back money that was correctly credited, so this is best-effort
    # and never propagates.
    referred_name = distributor.full_name or str(distributor.phone_number)
    try:
        send_sms(
            str(distributor.sponsor.phone_number),
            render_or_default(
                NotificationTemplate.Key.DIRECT_REFERRAL_BONUS_CREDITED_SMS,
                {"amount": bonus, "referred_name": referred_name},
                default_body=(
                    "You've earned GHS {{amount}} Direct Referral Bonus from "
                    "{{referred_name}}'s purchase. Check your Bancostore "
                    "wallet!"
                ),
            ),
        )
    except Exception:
        logger.exception(
            "consume_paid_starter_pack: failed to notify sponsor=%s of their "
            "GHS %s direct referral bonus -- credit already applied, "
            "notification only.",
            distributor.sponsor_id,
            bonus,
        )

    # Task 21d-ii: Section 6.6's "a referral bonus was paid instantly".
    send_notification(
        distributor.sponsor,
        Notification.EventType.REFERRAL_BONUS_PAID,
        render_or_default(
            NotificationTemplate.Key.DIRECT_REFERRAL_BONUS_CREDITED_INAPP,
            {"amount": bonus, "referred_name": referred_name},
            default_body=(
                "You earned GHS {{amount}} Direct Referral Bonus from "
                "{{referred_name}}'s purchase!"
            ),
        ),
    )


# Didit's own overall session status -> our Status choices. Any other value
# (e.g. "Not Started"/"In Progress") means the hosted flow isn't finished
# yet -- nothing final to store.
_DIDIT_STATUS_MAP = {
    "Approved": DiditVerification.Status.APPROVED,
    "Declined": DiditVerification.Status.DECLINED,
    "In Review": DiditVerification.Status.IN_REVIEW,
}


# Confirmed 2026-07-14 against a real live Didit verification session: Didit
# does NOT serve document/selfie images from a didit.me (sub)domain -- it
# redirects to a specific S3 bucket it controls. Allowing the exact host
# (not a broad *.amazonaws.com suffix, which anyone can get a bucket on)
# keeps this an allowlist rather than reopening the SSRF hole this guard
# exists to close.
_ALLOWED_MEDIA_HOSTS = frozenset(
    {
        "didit.me",
        "service-didit-verification-production-a1c5f9b8.s3.amazonaws.com",
    }
)
_ALLOWED_MEDIA_HOST_SUFFIX = ".didit.me"


def _assert_safe_media_url(url):
    """SSRF guard: these URLs come from Didit's own decision response, not
    directly from a user-typed field, but the server still shouldn't
    blindly fetch whatever string appears there -- a Didit-side bug, a
    MITM, or a compromised session could otherwise point this at an
    internal service (cloud metadata, localhost, a private IP). Requires
    https, an exact match against _ALLOWED_MEDIA_HOSTS (or a didit.me
    subdomain), and a resolved IP that's actually public."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"refusing to fetch non-https media URL: {url!r}")
    hostname = parsed.hostname or ""
    if hostname not in _ALLOWED_MEDIA_HOSTS and not hostname.endswith(
        _ALLOWED_MEDIA_HOST_SUFFIX
    ):
        raise ValueError(
            f"refusing to fetch media URL from unexpected host: {hostname!r}"
        )
    try:
        resolved_ip = ipaddress.ip_address(socket.gethostbyname(hostname))
    except (socket.gaierror, ValueError) as exc:
        raise ValueError(f"could not resolve media URL host: {hostname!r}") from exc
    if not resolved_ip.is_global:
        raise ValueError(
            f"refusing to fetch media URL resolving to a non-public IP: {resolved_ip}"
        )


def _download_image(url):
    """Fetch an image from one of Didit's short-lived media URLs and wrap
    it in a ContentFile that resize_and_convert_to_webp can read (it just
    needs something Image.open() accepts plus a .name)."""
    _assert_safe_media_url(url)
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    name = url.split("?")[0].rsplit("/", 1)[-1] or "image.jpg"
    return ContentFile(response.content, name=name)


def _apply_decision_to_verification(verification, decision, session_id) -> bool:
    """Parses a Didit decision response and updates `verification`'s fields
    in place (caller is responsible for `.save()`). Returns False if
    nothing should be stored yet (still in progress, or the response
    couldn't be parsed), True once fields have been applied.

    Didit's response is third-party data -- untrusted shape, not just
    untrusted content. The parsing below assumes id_verifications/
    face_matches/liveness_checks are lists of dicts (per Didit's
    documented response, confirmed 2026-07-14 against a real live
    verification session), but a malformed or unexpected response (a bug
    on Didit's side, or a future field-name change) could still make any
    of these something else entirely. Catching broadly here keeps
    consume_didit_result's "never raises" contract (matching
    consume_paid_starter_pack/consume_paid_registration) even against a
    response shape we didn't anticipate. Never logs the decision payload
    itself -- it carries extracted PII (name, document number, date of
    birth)."""
    mapped_status = _DIDIT_STATUS_MAP.get(decision.get("status"))
    if mapped_status is None:
        return False  # Still in progress -- nothing final to store yet.

    try:
        id_verification = (decision.get("id_verifications") or [{}])[0]
        face_match = (decision.get("face_matches") or [{}])[0]
        liveness_check = (decision.get("liveness_checks") or [{}])[0]
        if not all(
            isinstance(x, dict) for x in (id_verification, face_match, liveness_check)
        ):
            raise TypeError("expected dict entries in Didit's decision response")
    except (TypeError, KeyError, IndexError):
        logger.exception(
            "consume_didit_result: unexpected decision response shape for "
            "session_id=%s -- cannot parse. Needs manual investigation.",
            session_id,
        )
        return False

    verification.status = mapped_status
    verification.id_verification_status = id_verification.get("status", "")
    verification.face_match_status = face_match.get("status", "")
    verification.face_match_score = face_match.get("score")
    verification.liveness_status = liveness_check.get("status", "")
    verification.liveness_score = liveness_check.get("score")
    verification.extracted_full_name = id_verification.get("full_name") or ""
    verification.extracted_document_number = (
        id_verification.get("document_number") or ""
    )
    verification.warnings = decision.get("warnings") or []

    dob_raw = id_verification.get("date_of_birth")
    if dob_raw:
        try:
            verification.extracted_date_of_birth = datetime.strptime(
                dob_raw, "%Y-%m-%d"
            ).date()
        except ValueError:
            # Never log dob_raw itself -- it's PII (a real date of birth),
            # and the docstring above promises this function never logs
            # decision-payload PII.
            logger.warning(
                "consume_didit_result: unexpected date_of_birth format for "
                "session_id=%s (value omitted, contains PII)",
                session_id,
            )

    # front_image/back_image field names confirmed 2026-07-14 against a real
    # live Didit verification session; a missing key just means no image
    # gets stored, it doesn't crash.
    #
    # selfie_image bug (found 2026-07-23 via a real live session): this
    # used to read id_verification["portrait_image"] -- that's the ID
    # document's own embedded photo crop, not the live selfie the
    # distributor actually captured. liveness_check["reference_image"] is
    # the real live capture. Getting this wrong defeats manual KYC review
    # entirely: an admin comparing "the selfie" against the ID photo was
    # actually comparing the ID photo against itself.
    for source, field_name, url_key in (
        (id_verification, "id_front_image", "front_image"),
        (id_verification, "id_back_image", "back_image"),
        (liveness_check, "selfie_image", "reference_image"),
    ):
        url = source.get(url_key)
        if not url:
            continue
        try:
            downloaded = _download_image(url)
        except (requests.RequestException, ValueError):
            logger.exception(
                "consume_didit_result: failed to download %s for " "session_id=%s",
                url_key,
                session_id,
            )
            continue
        setattr(verification, field_name, resize_and_convert_to_webp(downloaded))

    return True


def consume_didit_result(session_id: str) -> None:
    """Task 11b: the single source of truth for turning a completed Didit
    verification session into a stored `DiditVerification` result. Called
    from both the callback-redirect view and the webhook -- whichever
    arrives first completes it; both call sites, and repeat calls with the
    same session_id, are always safe (idempotent).

    Never trusts a caller's claims about the result -- always re-fetches
    via get_session_decision() before storing anything, the same "always
    re-verify server-side" rule already applied to Paystack. Purely
    informational: never touches `Distributor.kyc_status` (only an admin's
    explicit action does, Task 11c -- see SPEC.md's "never auto-approve
    KYC" boundary).
    """

    def _attempt():
        with transaction.atomic():
            try:
                verification = select_for_update_nowait_if_supported(
                    DiditVerification.objects.filter(session_id=session_id)
                ).get()
            except DiditVerification.DoesNotExist:
                logger.error(
                    "consume_didit_result: no DiditVerification found for "
                    "session_id=%s -- a result may have arrived with no "
                    "matching record. Needs manual investigation.",
                    session_id,
                )
                return

            if verification.status != DiditVerification.Status.PENDING:
                return  # Already consumed -- idempotent no-op.

            try:
                decision = get_session_decision(session_id)
            except DiditError:
                logger.exception(
                    "consume_didit_result: Didit get_session_decision failed "
                    "for session_id=%s",
                    session_id,
                )
                return

            if _apply_decision_to_verification(verification, decision, session_id):
                verification.save()
                # Significant business event -- the on-call question this
                # answers: "did this distributor's KYC verification finish,
                # and with what result?" Never logs PII (name, document
                # number, DOB) -- just identifiers and the outcome.
                logger.info(
                    "consume_didit_result: session_id=%s distributor_id=%s "
                    "status=%s",
                    session_id,
                    verification.distributor_id,
                    verification.status,
                )

    retry_on_lock_contention(_attempt)


def approve_kyc(distributor) -> None:
    """Task 11c: the only place `Distributor.ir_id`/`kyc_status` transition
    to approved. Never called from anywhere else -- Didit's own result
    (including its "in_review" state) is informational only, per SPEC.md's
    "never auto-approve KYC" boundary; only this explicit admin action
    approves anyone.

    IR ID generation was run through doubt-driven-development 2026-07-13
    (see tasks/todo.md Task 11c): the sequence row is pre-seeded by a data
    migration rather than lazily created, sidestepping a cold-start race
    entirely rather than reasoning about whether a retry wrapper covers
    every shape of it. The idempotency guard checks both `kyc_status` and
    `ir_id` (not just one), since a future flow that resets `kyc_status`
    away from approved without touching `ir_id` would otherwise mint a
    second, ID-losing IR ID for the same distributor. Approving a
    previously-`rejected` distributor is deliberately allowed -- rejection
    isn't final; an admin can approve a fixed resubmission.
    """

    def _attempt():
        with transaction.atomic():
            try:
                locked = select_for_update_nowait_if_supported(
                    Distributor.objects.filter(pk=distributor.pk)
                ).get()
            except Distributor.DoesNotExist:
                logger.error(
                    "approve_kyc: distributor pk=%s no longer exists -- "
                    "cannot approve. Needs manual investigation.",
                    distributor.pk,
                )
                return False

            if (
                locked.kyc_status == Distributor.KycStatus.APPROVED
                or locked.ir_id is not None
            ):
                return False  # Already approved -- idempotent no-op.

            try:
                sequence = select_for_update_nowait_if_supported(
                    IrIdSequence.objects.filter(pk=1)
                ).get()
            except IrIdSequence.DoesNotExist:
                logger.error(
                    "approve_kyc: IrIdSequence row (pk=1) does not exist -- "
                    "migration 0011_seed_ir_id_sequence should have created "
                    "it. Cannot approve distributor pk=%s. Needs manual "
                    "investigation.",
                    distributor.pk,
                )
                return False
            number = sequence.next_number
            max_number = 10**config.IR_ID_NUMBER_OF_DIGITS - 1
            if number > max_number:
                # A genuine operational emergency -- the business cannot
                # onboard another distributor until IR_ID_NUMBER_OF_DIGITS
                # is increased. Logged loudly (not just raised) so it's
                # findable in structured logs, not just a bare traceback.
                logger.critical(
                    "approve_kyc: IR ID sequence exhausted (next_number=%s, "
                    "IR_ID_NUMBER_OF_DIGITS=%s) -- cannot approve "
                    "distributor pk=%s. Increase IR_ID_NUMBER_OF_DIGITS "
                    "immediately.",
                    number,
                    config.IR_ID_NUMBER_OF_DIGITS,
                    distributor.pk,
                )
                raise IrIdSequenceExhausted(
                    f"IR ID sequence exhausted: next_number={number} exceeds "
                    f"what IR_ID_NUMBER_OF_DIGITS={config.IR_ID_NUMBER_OF_DIGITS} "
                    "digits can represent. Increase that setting before "
                    "approving more distributors."
                )
            sequence.next_number = number + 1
            sequence.save(update_fields=["next_number"])

            locked.ir_id = (
                f"{config.IR_ID_PREFIX}"
                f"{str(number).zfill(config.IR_ID_NUMBER_OF_DIGITS)}"
            )
            locked.kyc_status = Distributor.KycStatus.APPROVED
            locked.save(update_fields=["ir_id", "kyc_status"])
            # Significant, financially/legally relevant business event --
            # who approved is captured separately via Distributor.history
            # (django-simple-history + HistoryRequestMiddleware), this log
            # line is for fast log-based searching/alerting.
            logger.info(
                "approve_kyc: distributor pk=%s approved, ir_id=%s",
                locked.pk,
                locked.ir_id,
            )
            return True

    approved_now = retry_on_lock_contention(_attempt)
    if approved_now:
        # Task 21d-ii: Section 6.6's "their KYC was approved or rejected".
        # Guarded on the real transition, not just a successful call --
        # the idempotent no-op case above must never double-notify.
        send_notification(
            distributor,
            Notification.EventType.KYC_DECIDED,
            render_or_default(
                NotificationTemplate.Key.KYC_APPROVED,
                {},
                default_body="Your KYC verification has been approved!",
            ),
        )


def reject_kyc(distributor, reason: str) -> None:
    """Task 11c: rejects a pending (or previously-rejected) distributor's
    KYC with a reason, from the admin's own judgment -- Didit's result is
    shown as context only. Refuses to reject an already-`approved`
    distributor: a permanent IR ID, once assigned, isn't something this
    function un-does; a real "revoke approval" flow would be a separate,
    more deliberate feature."""

    def _attempt():
        with transaction.atomic():
            try:
                locked = select_for_update_nowait_if_supported(
                    Distributor.objects.filter(pk=distributor.pk)
                ).get()
            except Distributor.DoesNotExist:
                logger.error(
                    "reject_kyc: distributor pk=%s no longer exists -- "
                    "cannot reject. Needs manual investigation.",
                    distributor.pk,
                )
                return False

            if locked.kyc_status == Distributor.KycStatus.APPROVED:
                logger.warning(
                    "reject_kyc: distributor pk=%s is already approved -- "
                    "refusing to reject an approved distributor.",
                    locked.pk,
                )
                return False

            locked.kyc_status = Distributor.KycStatus.REJECTED
            locked.kyc_rejection_reason = reason
            locked.save(update_fields=["kyc_status", "kyc_rejection_reason"])
            # Who rejected + the reason text are in Distributor.history
            # (django-simple-history); this log line is just for fast
            # log-based searching/alerting, so it deliberately omits the
            # reason text itself.
            logger.info("reject_kyc: distributor pk=%s rejected", locked.pk)
            return True

    rejected_now = retry_on_lock_contention(_attempt)
    if rejected_now:
        # Task 21d-ii: Section 6.6's "their KYC was approved or rejected".
        send_notification(
            distributor,
            Notification.EventType.KYC_DECIDED,
            render_or_default(
                NotificationTemplate.Key.KYC_REJECTED,
                {"reason": reason},
                default_body="Your KYC verification was rejected: {{reason}}",
            ),
        )


def send_password_reset_code(phone_number: str) -> None:
    """Task 63b / 65. Sends a password reset code if the number has an
    account: by SMS, or by email if the text fails and the account has an
    address. Never raises and returns nothing, so no caller can reveal which
    case happened.

    Runs in the Celery worker (send_password_reset_code_task), never in the
    request: the lookup and the ~1 second mNotify call happen only for real
    accounts, so doing them in the request let anyone tell registered
    numbers apart by how long the page took (Task 65)."""
    distributor = (
        Distributor.objects.select_related("user")
        .filter(phone_number=phone_number)
        .first()
    )
    if distributor is None:
        return
    try:
        generate_otp(
            phone_number,
            purpose="password_reset",
            fallback_email=distributor.user.email,
        )
    except OtpDeliveryFailed:
        # Already logged in generate_otp. Retrying the same job can't help:
        # the distributor can ask for a new code, and the admin has already
        # been alerted if credit ran out.
        pass
