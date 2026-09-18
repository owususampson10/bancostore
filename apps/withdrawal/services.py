import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models.functions import Now
from django.utils import timezone

from constance import config

from apps.distributors.models import Distributor
from apps.distributors.paystack import (
    MOBILE_MONEY_BANK_CODES,
    PaystackNotFoundError,
    create_transfer_recipient,
    initiate_transfer,
    verify_transfer,
)
from apps.notifications.models import Notification, NotificationTemplate
from apps.notifications.rendering import render_or_default
from apps.notifications.services import send_notification
from apps.notifications.sms import send_sms
from apps.wallet.models import WalletTransaction
from apps.wallet.services import InsufficientBalanceError, credit, debit
from bancostore.concurrency import (
    retry_on_lock_contention,
    select_for_update_nowait_if_supported,
)

from .models import WithdrawalRequest

logger = logging.getLogger(__name__)

# Task 16c (doubt-driven-development, pre-implementation review): only
# "weekly" is meaningful today. WITHDRAWAL_FREQUENCY now has a bounded
# constance widget (apps/platform_settings/config.py) restricting the
# admin-editable value to exactly this key, so the KeyError path below is
# unreachable through the admin UI -- it stays as a loud failure, not a
# silent one, for any other path that might set the setting (e.g. a data
# migration, direct DB edit).
WITHDRAWAL_FREQUENCY_DURATIONS = {"weekly": timedelta(days=7)}

# A request still in one of these states counts against the once-per-
# WITHDRAWAL_FREQUENCY window. Deliberately excludes REJECTED and
# PAYOUT_FAILED_REVERSED (ADR-0004 point 11, user-confirmed): an outcome
# outside the distributor's control shouldn't cost them their window.
WINDOW_BLOCKING_STATUSES = [
    WithdrawalRequest.Status.SUBMITTED,
    WithdrawalRequest.Status.APPROVED_DEBITED,
    WithdrawalRequest.Status.QUEUED_FOR_PAYOUT,
    WithdrawalRequest.Status.PAID,
]


class KycNotApproved(Exception):
    pass


class PayoutDestinationNotSet(Exception):
    pass


class PayoutRecipientNameNotSet(Exception):
    """CodeRabbit finding, PR #19: create_transfer_recipient (Task 16e)
    requires a name, and Distributor.full_name/DiditVerification
    .extracted_full_name can both be blank -- rare (KYC approval implies
    a submitted DiditVerification exists, but Didit's own OCR extraction
    can still fail to find a name while face-match/liveness still pass),
    but real. Without this check, approve_withdrawal_request would debit
    the wallet for a request that's guaranteed to fail unclearly at
    Paystack later (Task 16f), leaving it debited and stuck with no
    automatic recovery path."""

    pass


class BelowMinimumAmount(Exception):
    pass


class AboveMaximumAmount(Exception):
    pass


class InsufficientWalletBalance(Exception):
    pass


class WithdrawalWindowActive(Exception):
    pass


class WithdrawalFrequencyMisconfigured(Exception):
    pass


class WithholdingTaxMisconfigured(Exception):
    pass


class WithdrawalRequestNotFound(Exception):
    pass


class WithdrawalRequestNotPending(Exception):
    """Raised when approve/reject is attempted on a request that's no
    longer submitted -- another admin already approved/rejected it, or
    (approve only) it's already further along (queued_for_payout/paid).

    Deliberately NOT idempotent-silent like apps.distributors.services
    .approve_kyc's precedent (which returns early on an already-approved
    KYC): this is money-moving, and a bulk admin action's results summary
    should be able to show "already handled by someone else" explicitly
    rather than silently treating a stale action as a success. `status`
    carries the request's actual current status so a caller can build
    that distinction without parsing the message string."""

    def __init__(self, message, *, status):
        super().__init__(message)
        self.status = status


def _notify(phone_number, message, *, context):
    """Task 16g. Best-effort SMS notification -- mirrors
    apps.distributors.services._credit_direct_referral_bonus's own
    established try/except pattern (Task 12): the underlying state
    change has already committed by the time this is called, so an SMS
    provider outage must never roll it back, and never propagates.

    Deliberately called by every caller here AFTER retry_on_lock_
    contention returns, never from inside a caller's own _attempt()
    closure -- unlike Task 12's precedent (which sends from inside the
    lock), this project's own Task 16f principle is that no external
    HTTP call should ever happen while holding a row lock (see
    claim_for_payout's docstring). A stricter standard than Task 12's
    own shipped code, not a contradiction of it -- Task 12 isn't touched
    here, out of scope for this task."""
    try:
        send_sms(str(phone_number), message)
    except Exception:
        logger.exception(
            "%s: failed to send withdrawal notification -- state change "
            "already committed, notification only.",
            context,
        )


def submit_withdrawal_request(distributor, amount: Decimal) -> WithdrawalRequest:
    """Task 16c. The sole entry point for creating a WithdrawalRequest --
    no other code path should call WithdrawalRequest.objects.create()
    directly. Does NOT touch the wallet (ADR-0004 decision 3 -- debit
    happens at admin approval, Task 16d).

    Locks the Distributor row for the whole check-and-create sequence
    (doubt-driven-development, pre-implementation review caught that a
    bare .exists()-then-.create() has a real TOCTOU race under concurrent
    submissions -- proven with a 5-thread test, mirroring
    apps/wallet/services.py::debit()'s own concurrency proof). Matches
    this codebase's established "lock Distributor first" convention
    (apps/commissions/services.py::process_binary_bonus_for_distributor),
    so a future caller combining this with a commission cycle for the
    same distributor can't deadlock on reversed lock ordering.

    Raises a distinct exception per rejection reason -- see the classes
    above -- rather than one generic error, so the view layer can render
    a clear, specific message for each."""
    if not isinstance(amount, Decimal):
        raise TypeError(
            f"submit_withdrawal_request() amount must be a Decimal, got "
            f"{type(amount).__name__}"
        )
    # Quantized up front (doubt-driven-development finding): tax_amount is
    # always quantized below, but amount previously wasn't -- letting a
    # caller pass more than 2 decimal places would give amount/tax_amount
    # inconsistent rounding at INSERT time (the DB backend re-quantizes
    # amount using its own default rounding, not ROUND_HALF_UP), risking
    # an opaque IntegrityError from the model's own arithmetic
    # CheckConstraint instead of a clean, predictable value.
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def _attempt():
        with transaction.atomic():
            locked = select_for_update_nowait_if_supported(
                Distributor.objects.filter(pk=distributor.pk)
            ).get()

            if locked.kyc_status != Distributor.KycStatus.APPROVED:
                raise KycNotApproved(
                    f"distributor {locked.pk} does not have approved KYC"
                )
            if not locked.has_payout_destination:
                raise PayoutDestinationNotSet(
                    f"distributor {locked.pk} has no payout destination set"
                )
            if amount < config.MIN_WITHDRAWAL_AMOUNT:
                raise BelowMinimumAmount(
                    f"{amount} is below the minimum of {config.MIN_WITHDRAWAL_AMOUNT}"
                )
            if amount > config.MAX_WITHDRAWAL_AMOUNT:
                raise AboveMaximumAmount(
                    f"{amount} is above the maximum of {config.MAX_WITHDRAWAL_AMOUNT}"
                )

            # MIN/MAX_WITHDRAWAL_AMOUNT bound a single request in
            # isolation, not against any individual balance (ADR-0004
            # point 5) -- a distributor with GHS 150 could otherwise
            # request GHS 9,000 and pass both bounds cleanly.
            wallet = getattr(locked, "wallet", None)
            balance = wallet.balance if wallet else Decimal("0")
            if amount > balance:
                raise InsufficientWalletBalance(
                    f"distributor {locked.pk} balance {balance} is less than "
                    f"requested {amount}"
                )

            frequency = str(config.WITHDRAWAL_FREQUENCY).strip().lower()
            try:
                window = WITHDRAWAL_FREQUENCY_DURATIONS[frequency]
            except KeyError:
                logger.error(
                    "submit_withdrawal_request: unknown WITHDRAWAL_FREQUENCY=%r "
                    "-- every withdrawal submission will fail until this is "
                    "corrected in admin settings",
                    config.WITHDRAWAL_FREQUENCY,
                )
                raise WithdrawalFrequencyMisconfigured(
                    f"Unknown WITHDRAWAL_FREQUENCY: {config.WITHDRAWAL_FREQUENCY!r}"
                )

            # Compared DB-side (Now(), the database server's own clock),
            # not Python's timezone.now() -- doubt-driven-development
            # review: comparing a Python-computed "now" against
            # created_at (itself set by a Python-computed auto_now_add on
            # whichever app server handled the first request) risks a
            # boundary that shifts with clock skew across worker
            # processes. A single DB clock for both sides of the
            # comparison removes that dependency entirely.
            if WithdrawalRequest.objects.filter(
                distributor=locked,
                status__in=WINDOW_BLOCKING_STATUSES,
                created_at__gt=Now() - window,
            ).exists():
                raise WithdrawalWindowActive(
                    f"distributor {locked.pk} already has a withdrawal request "
                    f"within the current {frequency} window"
                )

            tax_amount = (
                amount * config.WITHHOLDING_TAX_RATE / Decimal("100")
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            net_amount = amount - tax_amount
            # Defensive bound (doubt-driven-development finding, extended
            # by code-review-and-quality 2026-07-23): WITHHOLDING_TAX_RATE
            # now has a bounded admin widget (percentage_field, 0-100),
            # but this stays as defense-in-depth against any other write
            # path. The original check only covered net_amount < 0 (rate
            # above 100%) -- a negative rate produces tax_amount < 0
            # instead, with net_amount > 0 (since net_amount = amount -
            # tax_amount, and amount == tax_amount + net_amount always
            # holds by construction regardless of sign), so it sailed
            # straight past the old single check and hit the model's
            # CheckConstraint as an opaque IntegrityError from the other
            # direction. Both signs are checked now.
            if tax_amount < 0 or net_amount < 0:
                logger.error(
                    "submit_withdrawal_request: WITHHOLDING_TAX_RATE=%s produced "
                    "tax_amount=%s net_amount=%s for amount=%s -- check platform "
                    "settings",
                    config.WITHHOLDING_TAX_RATE,
                    tax_amount,
                    net_amount,
                    amount,
                )
                raise WithholdingTaxMisconfigured(
                    "WITHHOLDING_TAX_RATE is misconfigured -- withdrawals are "
                    "temporarily unavailable"
                )

            request = WithdrawalRequest.objects.create(
                distributor=locked,
                amount=amount,
                tax_amount=tax_amount,
                net_amount=net_amount,
            )
            logger.info(
                "submit_withdrawal_request: distributor_id=%s amount=%s "
                "tax_amount=%s net_amount=%s",
                locked.pk,
                amount,
                tax_amount,
                net_amount,
            )
            return request

    return retry_on_lock_contention(_attempt)


def approve_withdrawal_request(withdrawal_request, *, reviewed_by) -> WithdrawalRequest:
    """Task 16d. Admin approves a WithdrawalRequest at status=submitted,
    transitioning it to approved_debited and debiting the distributor's
    wallet for net_amount -- the first real caller of
    apps.wallet.services.debit(). Takes a WithdrawalRequest instance (not
    an id), matching this codebase's own established service-function
    convention (debit(distributor, ...), submit_withdrawal_request
    (distributor, ...)) -- the instance is re-fetched and locked
    internally regardless, so this only affects the calling convention,
    not correctness.

    Locks the Distributor row first (matching process_binary_bonus_for_
    distributor/process_matching_bonus_for_distributor's existing order
    exactly, so this can never deadlock against those batch drivers for
    the same distributor), then locks the WithdrawalRequest row itself
    before checking status (doubt-driven-development, pre-implementation
    review: an earlier draft checked status against a stale, unlocked
    snapshot taken before any locking happened -- two concurrent
    approvals could both pass that check, with the actual double-debit
    protection then falling entirely to apps.wallet.models.
    WalletTransaction's own unique constraint in a different app,
    surfacing as an undocumented raw IntegrityError instead of a clean,
    catchable exception here). Re-checks kyc_status AND
    has_payout_destination inside the lock -- both can change between
    submission and approval -- before snapshotting the payout destination
    onto the request (ADR-0004 point 7: never read live off Distributor
    again after this point) and calling debit().

    apps.wallet.services.InsufficientBalanceError is caught and re-raised
    as this module's own InsufficientWalletBalance (the same exception
    submit_withdrawal_request already uses for the equivalent submission-
    time check) so a caller only ever needs to catch withdrawal-module
    exceptions, never reach into the wallet module's own error types too.

    Raises WithdrawalRequestNotPending (see its own docstring for why
    this isn't idempotent-silent), WithdrawalRequestNotFound,
    KycNotApproved, PayoutDestinationNotSet, PayoutRecipientNameNotSet,
    or InsufficientWalletBalance."""
    if reviewed_by is None:
        raise TypeError("approve_withdrawal_request() reviewed_by must not be None")

    def _attempt():
        with transaction.atomic():
            distributor_id = (
                WithdrawalRequest.objects.filter(pk=withdrawal_request.pk)
                .values_list("distributor_id", flat=True)
                .first()
            )
            if distributor_id is None:
                raise WithdrawalRequestNotFound(
                    f"WithdrawalRequest {withdrawal_request.pk} does not exist"
                )

            locked_distributor = select_for_update_nowait_if_supported(
                Distributor.objects.filter(pk=distributor_id)
            ).get()
            locked_request = select_for_update_nowait_if_supported(
                WithdrawalRequest.objects.filter(pk=withdrawal_request.pk)
            ).get()

            if locked_request.status != WithdrawalRequest.Status.SUBMITTED:
                raise WithdrawalRequestNotPending(
                    f"WithdrawalRequest {locked_request.pk} is not pending "
                    f"(status={locked_request.status})",
                    status=locked_request.status,
                )
            if locked_distributor.kyc_status != Distributor.KycStatus.APPROVED:
                raise KycNotApproved(
                    f"distributor {locked_distributor.pk} KYC is no longer approved"
                )
            if not locked_distributor.has_payout_destination:
                raise PayoutDestinationNotSet(
                    f"distributor {locked_distributor.pk} has no payout "
                    f"destination set"
                )

            locked_request.payout_mobile_money_number = (
                locked_distributor.mobile_money_number
            )
            locked_request.payout_mobile_money_network = (
                locked_distributor.mobile_money_network
            )
            # Task 16f: create_transfer_recipient requires a name.
            # Mirrors kyc_review_detail/withdrawal_review_detail's own
            # fallback exactly -- a distributor with a blank full_name
            # (only ever set from PendingRegistration at registration
            # time) still has a real name Didit extracted from their ID.
            verification = getattr(locked_distributor, "didit_verification", None)
            locked_request.payout_recipient_name = locked_distributor.full_name or (
                verification.extracted_full_name if verification else ""
            )
            if not locked_request.payout_recipient_name:
                raise PayoutRecipientNameNotSet(
                    f"distributor {locked_distributor.pk} has no name available "
                    f"for the Paystack transfer recipient"
                )

            try:
                debit(
                    locked_distributor,
                    locked_request.net_amount,
                    transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
                    reference=f"withdrawal-{locked_request.pk}",
                )
            except InsufficientBalanceError as exc:
                raise InsufficientWalletBalance(
                    f"distributor {locked_distributor.pk} balance is insufficient "
                    f"to approve withdrawal request {locked_request.pk}"
                ) from exc

            locked_request.status = WithdrawalRequest.Status.APPROVED_DEBITED
            locked_request.reviewed_by = reviewed_by
            locked_request.reviewed_at = timezone.now()
            locked_request.save()
            logger.info(
                "approve_withdrawal_request: withdrawal_request_id=%s "
                "distributor_id=%s net_amount=%s reviewed_by_id=%s",
                locked_request.pk,
                locked_distributor.pk,
                locked_request.net_amount,
                reviewed_by.pk,
            )
            return locked_request

    approved = retry_on_lock_contention(_attempt)
    # Task 16g. Every successful return here is a genuine one-time
    # transition (this function is deliberately not idempotent-silent --
    # see WithdrawalRequestNotPending's own docstring), so there is no
    # no-op case to guard against double-notifying, unlike
    # apply_verified_transfer_outcome below.
    _notify(
        approved.distributor.phone_number,
        render_or_default(
            NotificationTemplate.Key.WITHDRAWAL_APPROVED,
            {
                "net_amount": approved.net_amount,
                "withdrawal_day": config.WITHDRAWAL_DAY.title(),
            },
            default_body=(
                "Your Bancostore withdrawal of GHS {{net_amount}} has been "
                "approved. Payout processes on {{withdrawal_day}} -- we'll "
                "notify you once it's paid."
            ),
        ),
        context="approve_withdrawal_request",
    )
    # Task 21d-ii: Section 6.6's "their withdrawal was approved and sent".
    send_notification(
        approved.distributor,
        Notification.EventType.WITHDRAWAL_APPROVED,
        f"Your withdrawal of GHS {approved.net_amount} has been approved.",
    )
    return approved


def reject_withdrawal_request(
    withdrawal_request, *, reviewed_by, reason
) -> WithdrawalRequest:
    """Task 16d. Requires a non-empty reason (mirrors the KYC reject
    flow's own requirement, apps.distributors.services::reject_kyc) and
    never touches the wallet under any code path. Locks only the
    WithdrawalRequest row (never Distributor -- there's nothing here that
    needs it), inside transaction.atomic(), so a concurrent
    approve_withdrawal_request on the same request can't have its
    committed fields (status, reviewed_by, reviewed_at, the payout
    snapshot) silently clobbered by a stale blind overwrite here."""
    if reviewed_by is None:
        raise TypeError("reject_withdrawal_request() reviewed_by must not be None")
    if not reason or not reason.strip():
        raise ValueError("reject_withdrawal_request() reason must not be empty")

    def _attempt():
        with transaction.atomic():
            try:
                locked_request = select_for_update_nowait_if_supported(
                    WithdrawalRequest.objects.filter(pk=withdrawal_request.pk)
                ).get()
            except WithdrawalRequest.DoesNotExist:
                raise WithdrawalRequestNotFound(
                    f"WithdrawalRequest {withdrawal_request.pk} does not exist"
                ) from None

            if locked_request.status != WithdrawalRequest.Status.SUBMITTED:
                raise WithdrawalRequestNotPending(
                    f"WithdrawalRequest {locked_request.pk} is not pending "
                    f"(status={locked_request.status})",
                    status=locked_request.status,
                )

            locked_request.status = WithdrawalRequest.Status.REJECTED
            locked_request.reviewed_by = reviewed_by
            locked_request.reviewed_at = timezone.now()
            locked_request.rejection_reason = reason
            locked_request.save()
            logger.info(
                "reject_withdrawal_request: withdrawal_request_id=%s "
                "reviewed_by_id=%s reason=%s",
                locked_request.pk,
                reviewed_by.pk,
                reason,
            )
            return locked_request

    rejected = retry_on_lock_contention(_attempt)
    # Task 16g. Same "not idempotent-silent, every success is a genuine
    # one-time transition" reasoning as approve_withdrawal_request above.
    _notify(
        rejected.distributor.phone_number,
        render_or_default(
            NotificationTemplate.Key.WITHDRAWAL_REJECTED,
            {"amount": rejected.amount, "reason": rejected.rejection_reason},
            default_body=(
                "Your Bancostore withdrawal request of GHS {{amount}} was "
                "not approved. Reason: {{reason}}"
            ),
        ),
        context="reject_withdrawal_request",
    )
    return rejected


def _generate_transfer_reference(withdrawal_request_id):
    """Stable across retries -- no timestamp/uuid component, so the same
    WithdrawalRequest always gets the same reference. That's what makes
    claim_for_payout idempotent, and lets Paystack's own reference-
    uniqueness enforcement double as a backstop against an accidental
    duplicate initiate_transfer call (doubt-driven-development finding).

    28 characters, safely inside Paystack's real 16-50 char lowercase-
    alphanumeric-dash-underscore requirement (confirmed against
    Paystack's own docs during Task 16e) regardless of how large the pk
    ever grows -- zero-padded to 10 digits."""
    return f"withdrawal-payout-{withdrawal_request_id:010d}"


def claim_for_payout(withdrawal_request) -> WithdrawalRequest:
    """Task 16f. Locked, fast, local-only status transition
    approved_debited -> queued_for_payout with a pinned Paystack
    transfer reference. No external HTTP call happens inside this lock
    (doubt-driven-development finding: holding a row lock across a
    Paystack round-trip -- up to REQUEST_TIMEOUT_SECONDS -- is a real
    denial-of-service vector against this project's limited worker
    pool, the same concern bancostore/concurrency.py's own module
    docstring exists to prevent). Callers make the actual Paystack calls
    afterward, with no lock held.

    Idempotent by construction: if the row is already queued_for_payout
    (already claimed, with a reference), this is a silent no-op that
    returns the existing request unchanged rather than re-claiming or
    regenerating the reference -- process_withdrawal_payout relies on
    this to call claim_for_payout unconditionally on every attempt,
    regardless of whether a prior attempt already claimed the row.

    Does NOT lock Distributor -- unlike approve_withdrawal_request, this
    function never reads anything off Distributor. The payout
    destination and recipient name are already snapshotted onto the
    request itself (approve_withdrawal_request, Task 16d/16f), so there
    is no Distributor-first lock-ordering question to resolve here.

    Raises WithdrawalRequestNotFound, or WithdrawalRequestNotPending if
    the row is in any other status (already paid/rejected/reversed) --
    an unexpected state at this point, not something to silently retry."""

    def _attempt():
        with transaction.atomic():
            try:
                locked_request = select_for_update_nowait_if_supported(
                    WithdrawalRequest.objects.filter(pk=withdrawal_request.pk)
                ).get()
            except WithdrawalRequest.DoesNotExist:
                raise WithdrawalRequestNotFound(
                    f"WithdrawalRequest {withdrawal_request.pk} does not exist"
                ) from None

            if locked_request.status == WithdrawalRequest.Status.QUEUED_FOR_PAYOUT:
                return locked_request

            if locked_request.status != WithdrawalRequest.Status.APPROVED_DEBITED:
                raise WithdrawalRequestNotPending(
                    f"WithdrawalRequest {locked_request.pk} is not ready for "
                    f"payout (status={locked_request.status})",
                    status=locked_request.status,
                )

            locked_request.paystack_transfer_reference = _generate_transfer_reference(
                locked_request.pk
            )
            locked_request.status = WithdrawalRequest.Status.QUEUED_FOR_PAYOUT
            locked_request.queued_for_payout_at = timezone.now()
            locked_request.save()
            logger.info(
                "claim_for_payout: withdrawal_request_id=%s reference=%s",
                locked_request.pk,
                locked_request.paystack_transfer_reference,
            )
            return locked_request

    return retry_on_lock_contention(_attempt)


# Recognized as failure outcomes needing a reversal -- any other
# verified_status (e.g. "pending") is a no-op, left queued_for_payout
# for a future check to resolve, not treated as either success or
# failure. Safe-by-default: an unrecognized status never causes an
# incorrect payment, only a request that stays queued longer than it
# should.
#
# code-review flag, not silently assumed complete: Paystack's List
# Transfers endpoint documents a wider status vocabulary --
# pending/success/failed/otp/abandoned/reversed/blocked/rejected/
# received -- and it's unconfirmed whether verify_transfer's own
# response uses the identical set. "otp"/"abandoned"/"blocked"/
# "rejected" may also be terminal-failure states that belong here.
# Verifying this needs a real failed/blocked/rejected transfer to
# observe, which the account-tier restriction tracked in memory
# (project_paystack_transfer_account_tier_blocked) currently prevents.
_TRANSFER_FAILURE_STATUSES = {"failed", "reversed"}


def apply_verified_transfer_outcome(
    withdrawal_request, verified_status
) -> WithdrawalRequest:
    """Task 16f. The SOLE place that transitions a request away from
    queued_for_payout -- called by both the transfer webhook handler and
    the batch driver's own verify_transfer resume path. This is a
    doubt-driven-development finding, not a stylistic choice: two
    independently-written check-then-write paths for the same
    transition is exactly the race that would let a duplicate webhook
    delivery, or a webhook racing the batch driver's own verification,
    double-reverse or double-transition a request. One locked, idempotent
    function used by both callers removes the race by construction
    instead of trying to make two separate implementations individually
    safe.

    Locks the WithdrawalRequest row and no-ops (returns the row
    unchanged) if it has already left queued_for_payout -- this is the
    idempotency guarantee duplicate webhook deliveries and a webhook
    racing the batch driver both rely on.

    `verified_status` must be Paystack's own verify_transfer(...)["status"]
    value -- never a webhook body's own unverified claim (see
    apps.distributors.paystack.verify_transfer's own docstring for why).
    "success" transitions to paid. "failed"/"reversed" credits the
    distributor back net_amount (transaction_type=WITHDRAWAL_REVERSAL,
    a reference stable per request -- WalletTransaction's own unique
    constraint on (wallet, reference, transaction_type) is a second,
    structural backstop against a genuine duplicate reversal slipping
    past this function's own lock-based idempotency check) and
    transitions to payout_failed_reversed. Any other status is a no-op.

    Raises WithdrawalRequestNotFound if the row no longer exists.

    Task 16g: sends an SMS notification exactly once per real transition
    (paid or reversed). The no-op case (row already left queued_for_payout
    -- a duplicate webhook delivery, or the batch driver revisiting a row
    the webhook already resolved) must NOT re-notify, so _attempt()'s
    return value carries which transition (if any) actually happened this
    call, distinct from the row itself -- the row alone can't tell a
    caller "I just paid this" from "this was already paid," the same
    ambiguity Task 16f PR 2's own doubt-driven review caught and fixed
    for paid/total_amount accounting in the batch driver."""

    def _attempt():
        with transaction.atomic():
            try:
                locked_request = select_for_update_nowait_if_supported(
                    WithdrawalRequest.objects.filter(pk=withdrawal_request.pk)
                ).get()
            except WithdrawalRequest.DoesNotExist:
                raise WithdrawalRequestNotFound(
                    f"WithdrawalRequest {withdrawal_request.pk} does not exist"
                ) from None

            if locked_request.status != WithdrawalRequest.Status.QUEUED_FOR_PAYOUT:
                return locked_request, None

            if verified_status == "success":
                locked_request.status = WithdrawalRequest.Status.PAID
                locked_request.save()
                logger.info(
                    "apply_verified_transfer_outcome: withdrawal_request_id=%s "
                    "verified_status=success -> paid",
                    locked_request.pk,
                )
                return locked_request, "paid"
            elif verified_status in _TRANSFER_FAILURE_STATUSES:
                credit(
                    locked_request.distributor,
                    locked_request.net_amount,
                    transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_REVERSAL,
                    reference=f"withdrawal-reversal-{locked_request.pk}",
                )
                locked_request.status = WithdrawalRequest.Status.PAYOUT_FAILED_REVERSED
                locked_request.save()
                logger.info(
                    "apply_verified_transfer_outcome: withdrawal_request_id=%s "
                    "verified_status=%s -> payout_failed_reversed, reversed "
                    "net_amount=%s",
                    locked_request.pk,
                    verified_status,
                    locked_request.net_amount,
                )
                return locked_request, "reversed"
            else:
                logger.info(
                    "apply_verified_transfer_outcome: withdrawal_request_id=%s "
                    "verified_status=%s -- not a terminal outcome, leaving "
                    "queued_for_payout",
                    locked_request.pk,
                    verified_status,
                )
                return locked_request, None

    updated_request, transition = retry_on_lock_contention(_attempt)

    if transition == "paid":
        _notify(
            updated_request.distributor.phone_number,
            render_or_default(
                NotificationTemplate.Key.WITHDRAWAL_PAID,
                {"net_amount": updated_request.net_amount},
                default_body=(
                    "Good news! Your Bancostore withdrawal of GHS "
                    "{{net_amount}} has been paid to your mobile money "
                    "account."
                ),
            ),
            context="apply_verified_transfer_outcome:paid",
        )
    elif transition == "reversed":
        _notify(
            updated_request.distributor.phone_number,
            render_or_default(
                NotificationTemplate.Key.WITHDRAWAL_REVERSED,
                {"net_amount": updated_request.net_amount},
                default_body=(
                    "Your Bancostore withdrawal of GHS {{net_amount}} could "
                    "not be completed and has been returned to your wallet. "
                    "You can request a new withdrawal anytime."
                ),
            ),
            context="apply_verified_transfer_outcome:reversed",
        )

    return updated_request


def _local_mobile_money_number(e164_number):
    """Paystack's create_transfer_recipient was live-verified during
    Task 16e to accept and echo back Ghana mobile money account numbers
    in the local "0..." format (account_number="0551234987"), not
    E.164 -- confirmed by a real successful sandbox call, not assumed.
    This codebase stores payout_mobile_money_number as E.164 ("+233...",
    PHONENUMBER_DEFAULT_REGION="GH" with the library's own default
    format), so it must be converted before every call."""
    return "0" + str(e164_number)[4:]


def process_withdrawal_payout(withdrawal_request) -> str:
    """Task 16f. Orchestrates one WithdrawalRequest's actual payout to
    Paystack -- outside any row lock (claim_for_payout's own lock never
    spans an external HTTP call, and neither does anything below it).
    Safe to call repeatedly for the same request -- a Celery retry, or
    the batch driver revisiting a row across cycles -- because every
    step here is itself idempotent:

    1. claim_for_payout() -- idempotent. If the row isn't
       approved_debited/queued_for_payout (already paid/rejected/
       reversed -- e.g. a webhook already resolved it before this call
       ran), that status is returned immediately with no Paystack calls
       at all, not treated as a failure.
    2. Ensures a Paystack transfer recipient exists, built from the
       request's OWN snapshotted payout fields (ADR-0004 point 7) --
       never read live off Distributor. Paystack itself deduplicates
       recipients by account_number, so calling this again on a retry
       is safe and cheap, not a race to guard against locally.
    3. Tries verify_transfer(reference) FIRST, not initiate_transfer --
       the doubt-driven-development fix for the original design's
       critical gap: a retry landing between the claim commit and the
       actual initiate_transfer call must not assume Paystack already
       has the transfer. PaystackNotFoundError (Paystack has never seen
       this reference -- the expected case on a genuinely first
       attempt, or a crash before step 3 ever went out) falls back to
       actually calling initiate_transfer with the same pinned
       reference. Whatever verified status this finds is applied via
       apply_verified_transfer_outcome, the sole place that transitions
       a request out of queued_for_payout.

    Returns the request's resulting WithdrawalRequest.Status value."""
    try:
        claimed = claim_for_payout(withdrawal_request)
    except WithdrawalRequestNotPending as exc:
        return exc.status

    # code-review finding: a prior version of this function called
    # create_transfer_recipient unconditionally, making one wasted
    # Paystack API call on every resume attempt even when the recipient
    # was already cached from a first attempt -- real cost on a request
    # the batch driver might revisit across several Friday cycles while
    # a slow transfer resolves. Skipping when already cached is safe
    # because the cached code was itself confirmed correct by an earlier
    # call to this same endpoint for this same request.
    if claimed.paystack_recipient_code:
        recipient_code = claimed.paystack_recipient_code
    else:
        recipient = create_transfer_recipient(
            name=claimed.payout_recipient_name,
            account_number=_local_mobile_money_number(
                claimed.payout_mobile_money_number
            ),
            bank_code=MOBILE_MONEY_BANK_CODES[claimed.payout_mobile_money_network],
        )
        recipient_code = recipient["recipient_code"]
        WithdrawalRequest.objects.filter(pk=claimed.pk).update(
            paystack_recipient_code=recipient_code
        )

    try:
        result = verify_transfer(claimed.paystack_transfer_reference)
    except PaystackNotFoundError:
        result = initiate_transfer(
            amount_pesewas=int(claimed.net_amount * 100),
            recipient_code=recipient_code,
            reference=claimed.paystack_transfer_reference,
            reason="Bancostore withdrawal payout",
        )

    updated = apply_verified_transfer_outcome(claimed, result["status"])
    return updated.status
