import logging
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import F, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from celery import shared_task

from apps.notifications.models import AdminNotification
from apps.notifications.services import send_admin_notification
from apps.orders.models import Order
from apps.orders.services import confirm_order_payment
from bancostore.concurrency import retry_on_lock_contention

from .models import Distributor, PaymentIssue, PendingRegistration
from .payment_issues import record_payment_issue
from .paystack import PaystackError, list_transactions, verify_transaction
from .services import (
    PendingRegistrationResolution,
    consume_didit_result,
    consume_paid_registration,
    consume_paid_starter_pack,
    resolve_unconsumed_pending_registration,
    send_password_reset_code,
)

logger = logging.getLogger(__name__)

# Task 67. See resolve_unconsumed_pending_registration for how these are
# used. 72 hours of an idle checkout (the incident's checkout was paid after
# 1h43m) and a hard 30 days, after which a row Paystack still can't give a
# definite answer on becomes a PaymentIssue rather than living forever.
PENDING_REGISTRATION_IDLE_TTL = timedelta(hours=72)
PENDING_REGISTRATION_MAX_AGE = timedelta(days=30)
# Bounds one run: at most this many Paystack calls (10s timeout each), well
# inside the 15 minutes before the next run starts.
CLEANUP_BATCH_SIZE = 50
# A row that was kept is not looked at again for this long, so rows Paystack
# keeps failing on can't sit at the front of every batch and starve the rest
# (code review, Task 67).
CLEANUP_RECHECK_INTERVAL = timedelta(hours=1)


def _eligible_for_cleanup(queryset, now):
    """The rows cleanup may look at: unconsumed, idle long enough (or past
    the hard age limit), and not already checked within the last hour.

    That last one is an exclude(), not filter(last_checked_at__lte=...), on
    purpose: last_checked_at is NULL for every row nobody has checked yet,
    which is most of them, and a filter would silently drop all of them and
    stop cleanup dead."""
    idle_cutoff = now - PENDING_REGISTRATION_IDLE_TTL
    return (
        queryset.filter(consumed_at__isnull=True)
        .filter(
            Q(payment_initialized_at__lte=idle_cutoff)
            | Q(payment_initialized_at__isnull=True, created_at__lte=idle_cutoff)
            | Q(created_at__lte=now - PENDING_REGISTRATION_MAX_AGE)
        )
        .exclude(last_checked_at__gt=now - CLEANUP_RECHECK_INTERVAL)
    )


def _claim_cleanup_candidates(now):
    """Picks this run's rows and stamps last_checked_at on them in one
    transaction, so an overlapping run takes different rows rather than
    repeating the same Paystack calls (CodeRabbit, PR #97).

    Two statements on purpose (agent code review): a plain SELECT shortlists
    the batch, then only those rows are locked. Locking the ordered
    candidate query itself would have InnoDB lock every row it reads -- the
    whole backlog during a Paystack outage, not the 50 taken -- so an
    overlapping run would SKIP LOCKED past all of them and claim nothing at
    all, which is worse than the duplication this exists to prevent.

    SKIP LOCKED where the backend has it (MySQL 8 in CI and production): a
    second run steps over rows the first already holds instead of blocking
    behind them. SQLite, used only for local dev and tests, has no row-level
    locking at all, so the guarantee is real in production but cannot be
    proven by the local suite -- the same honest limit as every other
    select_for_update in this codebase.

    last_checked_at is stamped with the batch's start time, before any
    Paystack call, so a worker killed mid-batch leaves its rows for the next
    hour rather than having them re-checked immediately. For a job whose
    entire cost is outbound API calls, that is the right way round."""

    def _claim():
        with transaction.atomic():
            shortlist = list(
                _eligible_for_cleanup(PendingRegistration.objects, now)
                .order_by(
                    F("last_checked_at").asc(nulls_first=True), "created_at", "pk"
                )
                .values_list("pk", flat=True)[:CLEANUP_BATCH_SIZE]
            )
            # Eligibility re-checked under the lock: a row another run
            # claimed between the two statements is not claimed twice.
            locked = _eligible_for_cleanup(
                PendingRegistration.objects.filter(pk__in=shortlist), now
            )
            if connection.features.has_select_for_update_skip_locked:
                locked = locked.select_for_update(skip_locked=True)
            claimed = list(locked.values_list("pk", flat=True))
            PendingRegistration.objects.filter(pk__in=claimed).update(
                last_checked_at=now
            )
            return claimed

    # Every other write in this codebase goes through this; without it a
    # single lock-wait timeout kills the whole cleanup cycle for 15 minutes.
    return retry_on_lock_contention(_claim)


@shared_task
def cleanup_expired_pending_registrations():
    """Removes abandoned PendingRegistration rows so onboarding PII isn't
    held indefinitely (confirmed with the user 2026-07-13) -- but, since
    Task 67, only once Paystack confirms the registration was never paid,
    or its payment has become an account or a recorded PaymentIssue.

    Before Task 67 this deleted every unconsumed row over an hour old
    without asking Paystack. On 2026-09-16 that removed a registration
    whose still-open checkout was paid 43 minutes later."""
    now = timezone.now()
    candidate_pks = _claim_cleanup_candidates(now)

    counts = {resolution: 0 for resolution in PendingRegistrationResolution}
    failed = 0
    for pk in candidate_pks:
        try:
            resolution = resolve_unconsumed_pending_registration(
                pk,
                idle_for=PENDING_REGISTRATION_IDLE_TTL,
                max_age=PENDING_REGISTRATION_MAX_AGE,
                delete_unconfirmed=True,
            )
        except Exception:
            failed += 1
            logger.exception(
                "cleanup_expired_pending_registrations: failed for "
                "pending_registration_id=%s",
                pk,
            )
            continue
        counts[resolution] += 1

    if candidate_pks:
        logger.info(
            "cleanup_expired_pending_registrations: checked=%s deleted=%s "
            "consumed=%s kept=%s failed=%s",
            len(candidate_pks),
            counts[PendingRegistrationResolution.DELETED],
            counts[PendingRegistrationResolution.CONSUMED],
            counts[PendingRegistrationResolution.KEPT],
            failed,
        )


# Task 67. How far back the daily reconciliation looks, and how recent a
# payment it leaves to the webhook (which normally lands within seconds).
# Task 68h. Widened from 7 days once the semantics were settled by
# experiment (2026-09-18): Paystack's `from` filter is on CREATED time, not
# paid time -- asking from 08:30 on the incident's own day returns the
# transactions created after 08:30 and NOT the one created at 07:56 and paid
# at 09:39. A 7-day window therefore could not see the very thing it exists
# to catch: a checkout paid long after it was opened. 30 days covers any
# realistic lag; the cost is API pages per run, bounded by
# RECONCILIATION_MAX_PAGES and paid once a day.
RECONCILIATION_WINDOW = timedelta(days=30)
RECONCILIATION_GRACE = timedelta(hours=1)
RECONCILIATION_PAGE_SIZE = 100
# A runaway pageCount must not turn one run into thousands of API calls.
RECONCILIATION_MAX_PAGES = 200


@shared_task
def reconcile_paystack_payments():
    """Task 67. Once a day, checks every payment Paystack marked successful
    in the last week against what it paid for, and makes sure each one either
    took effect or is a PaymentIssue the admin has been told about.

    The webhook is the normal path; this is the backstop for when it never
    arrived at all. On 2026-09-16 a registration fee was paid and nothing
    came of it, and the only trace was a log line -- this makes sure a
    payment like that is noticed within a day even if every other path
    missed it.

    A missed webhook is applied here first. A starter pack or order that
    still hasn't taken effect is only flagged after a second, direct check
    with Paystack succeeds and one more attempt to apply it fails:
    consume_paid_starter_pack and confirm_order_payment both return quietly
    on a Paystack timeout, and a false "refund this" alert would get a real
    purchase refunded.

    Known limit: a payment made more than a week after its checkout was
    opened may fall outside the listing, if Paystack's `from` filters on
    creation time (undocumented). The webhook, which Paystack retries for up
    to 72 hours, is what covers that case."""
    now = timezone.now()
    paid_before = now - RECONCILIATION_GRACE
    summary = {"checked": 0, "failed": 0}

    page = 1
    while page <= RECONCILIATION_MAX_PAGES:
        transactions, meta = list_transactions(
            status="success",
            page=page,
            per_page=RECONCILIATION_PAGE_SIZE,
            from_=now - RECONCILIATION_WINDOW,
        )
        for transaction_data in transactions:
            try:
                if _reconcile_one(transaction_data, paid_before):
                    summary["checked"] += 1
            except Exception:
                summary["failed"] += 1
                logger.exception(
                    "reconcile_paystack_payments: failed for reference=%s",
                    transaction_data.get("reference"),
                )
        page_count = meta.get("pageCount") or 1
        if not transactions or page >= page_count:
            break
        if page == RECONCILIATION_MAX_PAGES:
            # Task 68g: an alert, not just a log line -- the payments this
            # run never looked at are exactly the oldest ones in the window.
            _alert_reconciliation_truncated(page, page_count)
            break
        page += 1

    # Task 68i. The outbound direction, checked on the same daily pass.
    try:
        _alert_on_stuck_payouts(now)
    except Exception:
        summary["failed"] += 1
        logger.exception("reconcile_paystack_payments: stuck-payout check failed")

    logger.info(
        "reconcile_paystack_payments: checked=%s failed=%s pages=%s",
        summary["checked"],
        summary["failed"],
        page,
    )
    return summary


def _reconcile_one(transaction_data, paid_before):
    """Returns True if this payment was one of ours and was checked."""
    reference = transaction_data.get("reference") or ""
    paid_at = parse_datetime(transaction_data.get("paid_at") or "")
    if paid_at is None or paid_at > paid_before:
        return False
    if not reference.startswith(("reg-", "pack-", "order-")):
        # Task 68g: money this platform did not ask for -- a Paystack payment
        # page or inline checkout on the same merchant account. Previously
        # skipped in silence, which is indistinguishable from "nothing
        # happened". Transfers are money going out and are not this job's
        # business.
        if reference.startswith("withdrawal-payout-"):
            return False
        record_payment_issue(
            reference,
            PaymentIssue.Kind.UNRECOGNISED_PAYMENT,
            verified=transaction_data,
            detail="This payment did not come from a Bancostore checkout "
            "(its reference is not one this platform issues). It may be a "
            "Paystack payment page or link on the same account.",
        )
        return True
    if PaymentIssue.objects.filter(reference=reference).exists():
        return True

    if reference.startswith("reg-"):
        if not PendingRegistration.objects.filter(
            consumed_reference=reference
        ).exists():
            # Creates the account, or records the issue itself.
            consume_paid_registration(reference)
        return True

    if reference.startswith("pack-"):
        _apply_or_flag(
            reference,
            is_applied=lambda: Distributor.objects.filter(
                Q(starter_pack_confirmed_at__isnull=False)
                | Q(cooling_off_cancelled_at__isnull=False),
                starter_pack_payment_reference=reference,
            ).exists(),
            apply=consume_paid_starter_pack,
            kind=PaymentIssue.Kind.STARTER_PACK_NOT_APPLIED,
        )
        return True

    _apply_or_flag(
        reference,
        # confirmed_at, not status: an order confirmed and later cancelled or
        # refunded by the admin was applied, and its money handled already.
        is_applied=lambda: Order.objects.filter(
            payment_reference=reference, confirmed_at__isnull=False
        ).exists(),
        apply=confirm_order_payment,
        kind=PaymentIssue.Kind.ORDER_NOT_APPLIED,
    )
    return True


def _apply_or_flag(reference, *, is_applied, apply, kind):
    if is_applied():
        return
    apply(reference)
    if is_applied():
        return
    try:
        verified = verify_transaction(reference)
    except PaystackError:
        logger.warning(
            "reconcile_paystack_payments: could not re-check reference=%s; "
            "trying again on the next run.",
            reference,
            exc_info=True,
        )
        return
    if verified.get("status") != "success":
        return
    apply(reference)
    if is_applied():
        return
    record_payment_issue(
        reference,
        kind,
        verified=verified,
        detail="Paystack confirms this payment, but it has not taken effect "
        "on Bancostore after two attempts.",
    )


@shared_task
def consume_didit_result_task(session_id: str) -> None:
    """Task 11 (performance-optimization retrospective, 2026-07-14): Didit's
    own webhook docs (https://docs.didit.me/integration/webhooks) specify a
    5-second response timeout, 2 retries, then the delivery is dropped --
    and explicitly say to "return 2xx as soon as you have queued the work,
    do heavy processing asynchronously." consume_didit_result does a Didit
    API call plus up to 3 synchronous image downloads (10s timeout each),
    easily exceeding 5 seconds under any real network latency. This task
    just wraps that same function so apps/distributors/views.py::
    didit_webhook can enqueue it and return 200 immediately, instead of
    doing the work inline. The callback-redirect view (kyc_verification_
    callback) deliberately still calls consume_didit_result synchronously
    -- it's a browser redirect waiting on a "Confirming..." interstitial,
    not a third-party webhook with an enforced timeout."""
    consume_didit_result(session_id)


@shared_task
def send_password_reset_code_task(phone_number: str) -> None:
    """Task 65. Queued by forgot_password and resend_otp for EVERY number,
    known or not, so those pages do identical work whatever the answer --
    see send_password_reset_code for why."""
    send_password_reset_code(phone_number)


# Task 68i. How long a payout may sit waiting for Paystack's final answer
# before the admin is told. Two days clears a normal weekend queue without
# leaving a distributor's money in limbo unnoticed.
STUCK_PAYOUT_AFTER = timedelta(days=2)


def _alert_on_stuck_payouts(now):
    """Task 68i. A withdrawal is debited from the wallet at approval, and
    only Paystack's "failed"/"reversed" put it back. Their transfer
    vocabulary also includes otp/abandoned/blocked/rejected, any of which
    leave a request sitting in queued_for_payout with the distributor's
    money gone and nobody told.

    This does NOT reverse the wallet by itself: an unknown status can still
    turn into a real transfer later, and crediting the wallet for one that
    then completes would pay the distributor twice. A human decides; this
    makes sure a human knows. The PaymentIssue's unique reference means one
    alert per stuck payout, however many days it runs."""
    # Imported here, not at module level: apps.withdrawal.services imports
    # from this app's own paystack module, and a module-level import either
    # way round would be circular.
    from apps.withdrawal.models import WithdrawalRequest
    from apps.withdrawal.services import _generate_transfer_reference

    stuck = WithdrawalRequest.objects.select_related("distributor").filter(
        status=WithdrawalRequest.Status.QUEUED_FOR_PAYOUT,
        created_at__lte=now - STUCK_PAYOUT_AFTER,
    )
    for request in stuck:
        record_payment_issue(
            _generate_transfer_reference(request.pk),
            PaymentIssue.Kind.WITHDRAWAL_NOT_PAID,
            distributor=request.distributor,
            detail=(
                f"GHS {request.net_amount} was taken from this distributor's "
                "wallet when the withdrawal was approved, but Paystack has "
                "never confirmed the payout. Check the transfer in the "
                "Paystack dashboard: if it failed, reverse the withdrawal so "
                "the money goes back to their wallet; if it paid, mark the "
                "request paid."
            ),
        )


def _alert_reconciliation_truncated(pages_read, page_count):
    """Task 68g. Tells the admin when one run could not read the whole
    window. Never raises: the run's real work has already happened."""
    summary = (
        f"Payment reconciliation stopped after {pages_read} of {page_count} "
        f"pages. The oldest payments in the window were not checked. Raise "
        f"RECONCILIATION_MAX_PAGES or run it more often."
    )
    logger.warning("reconcile_paystack_payments: %s", summary)
    try:
        send_admin_notification(AdminNotification.EventType.PAYMENT_ISSUE, summary)
    except Exception:
        logger.exception(
            "reconcile_paystack_payments: could not raise the truncation alert"
        )
