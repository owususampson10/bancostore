import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models.functions import Now
from django.utils import timezone

from constance import config

from apps.distributors.models import Distributor
from apps.wallet.models import WalletTransaction
from apps.wallet.services import InsufficientBalanceError, debit
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
    KycNotApproved, PayoutDestinationNotSet, or InsufficientWalletBalance."""
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

    return retry_on_lock_contention(_attempt)


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

    return retry_on_lock_contention(_attempt)
