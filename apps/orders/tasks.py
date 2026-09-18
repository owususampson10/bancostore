import logging
from datetime import timedelta
from functools import reduce
from operator import or_

from django.core.cache import cache
from django.db.models import F, Q
from django.utils import timezone

from celery import shared_task
from constance import config

from .models import Order, OrderCycleFailure, OrderCycleRun
from .receipt_email import (
    RECEIPT_SKIP_STATUSES,
    RECEIPT_SWEEP_MAX_SWEEPS,
    send_order_receipt_email_task,
)
from .services import _auto_cancel_pending_order

# Task 62: send_order_receipt_email_task is DEFINED in receipt_email.py
# (defining it here and importing it into services.py would be circular,
# since this module already imports from services). Re-exported so
# Celery's autodiscovery of apps.orders.tasks registers it explicitly
# rather than relying on a transitive import.
__all__ = [
    "auto_cancel_unpaid_orders",
    "resend_missing_order_receipts",
    "send_order_receipt_email_task",
]

logger = logging.getLogger(__name__)

AUTO_CANCEL_TASK_NAME = "auto-cancel-unpaid-orders"
AUTO_CANCEL_LOCK_KEY = "orders:auto_cancel_lock"
# Per-order work here is a single locked status transition plus two fast
# notification API calls (SMS/email) -- much cheaper per row than
# Withdrawal's up-to-3-Paystack-calls-per-row budget, but the lock is
# still renewed every iteration (not sized for the whole cycle at once),
# matching every other batch driver's convention rather than a bespoke
# one-off number.
AUTO_CANCEL_LOCK_TIMEOUT_SECONDS = 5 * 60


# Bounds one run's Paystack calls (10s timeout each) well inside the 30
# minutes before the next run, and stops an unfinished mobile-money prompt
# being re-asked about every cycle for a fortnight.
AUTO_CANCEL_BATCH_SIZE = 100
AUTO_CANCEL_RECHECK_INTERVAL = timedelta(hours=6)


def _pending_order_ids_query(cutoff):
    """Ordered oldest-first, matching Withdrawal's own batch driver
    convention -- not a correctness requirement (this is one
    .iterator() snapshot processed once), but a deliberate, consistent
    fairness choice rather than leaving row order to whatever plan the
    database happens to pick."""
    # Task 68a (adversarial security review): each of these now costs one
    # blocking Paystack call, so a run is capped and an order already asked
    # about within RECHECK_INTERVAL waits its turn. Least-recently-checked
    # first, so nothing is starved.
    recheck_cutoff = timezone.now() - AUTO_CANCEL_RECHECK_INTERVAL
    return iter(
        Order.objects.filter(status=Order.Status.PENDING, created_at__lt=cutoff)
        .exclude(payment_checked_at__gt=recheck_cutoff)
        .order_by(F("payment_checked_at").asc(nulls_first=True), "created_at")
        .values_list("pk", flat=True)[:AUTO_CANCEL_BATCH_SIZE]
    )


def _persist_auto_cancel_audit_record(
    *, run_at, evaluated, cancelled, failed, failures
):
    """Persists an OrderCycleRun (+ one OrderCycleFailure per real
    per-order exception) for one completed cycle -- called before the
    systemic-failure raise below, not after, mirroring
    apps.commissions.tasks._persist_cycle_audit_record /
    apps.withdrawal.tasks._persist_withdrawal_cycle_audit_record's own
    identical reasoning and best-effort fallback."""
    try:
        cycle_run = OrderCycleRun.objects.create(
            run_at=run_at,
            evaluated=evaluated,
            cancelled=cancelled,
            failed=failed,
        )
        if failures:
            OrderCycleFailure.objects.bulk_create(
                OrderCycleFailure(cycle_run=cycle_run, order_id=order_id, error=error)
                for order_id, error in failures
            )
    except Exception:
        logger.exception(
            "%s: failed to persist the audit-trail record for run_at=%s -- "
            "continuing regardless, since a missing audit row is a lesser "
            "problem than losing the raise/return that follows.",
            AUTO_CANCEL_TASK_NAME,
            run_at,
        )


@shared_task
def auto_cancel_unpaid_orders():
    """Task 18d (ADR-0006 decision 6). Batch driver cancelling every
    `pending` order older than the admin-editable
    PENDING_ORDER_AUTO_CANCEL_HOURS cutoff. Shape-mirrors
    apps.withdrawal.tasks.process_withdrawal_payouts (fixed run_at,
    per-iteration-renewed lock, per-order exception isolation, systemic-
    failure guard, audit record persisted before that guard's raise) but
    is deliberately a separate, hand-written driver, not a call into
    apps.commissions.tasks._run_commission_cycle -- that helper's
    process_one contract is `process_one(Distributor(pk=id), run_at) ->
    Decimal` (an amount earned); this iterates Order rows and needs "did
    this order get cancelled" (a bool), not an amount, and its audit
    model is OrderCycleRun/Failure, not CommissionCycleRun/Failure (see
    those models' own docstrings for why they're not merged into one
    job_name-discriminated pair -- this job moves no money at all).

    No PeriodicTask interval self-sync (unlike Binary/Matching Bonus):
    PENDING_ORDER_AUTO_CANCEL_HOURS is the age *cutoff* this task reads
    every run, not this task's own *run frequency* -- there is no
    second, separate "how often does this job run" constance setting to
    keep in sync, so there is nothing to self-heal here. The run
    frequency itself is seeded once via migration and expected to be
    changed, if ever, through django_celery_beat's own admin directly."""
    if not cache.add(AUTO_CANCEL_LOCK_KEY, "1", AUTO_CANCEL_LOCK_TIMEOUT_SECONDS):
        logger.warning(
            "%s: previous cycle still in progress (lock held) -- skipping "
            "this trigger rather than running a second concurrent cycle.",
            AUTO_CANCEL_TASK_NAME,
        )
        return {"skipped": True, "reason": "previous cycle still in progress"}

    try:
        run_at = timezone.now()
        cutoff = run_at - timedelta(hours=config.PENDING_ORDER_AUTO_CANCEL_HOURS)
        evaluated = 0
        cancelled = 0
        failed = 0
        failures = []  # [(order_id, error_message), ...]

        for order_id in _pending_order_ids_query(cutoff):
            evaluated += 1
            # Renews the lock's TTL on every iteration, matching every
            # other batch driver's convention -- a cycle still alive but
            # running past the timeout would otherwise lose the lock
            # mid-cycle, letting a second Beat trigger start a second,
            # genuinely concurrent cycle.
            cache.set(AUTO_CANCEL_LOCK_KEY, "1", AUTO_CANCEL_LOCK_TIMEOUT_SECONDS)
            try:
                if _auto_cancel_pending_order(order_id):
                    cancelled += 1
            except Exception as exc:
                failed += 1
                failures.append((order_id, str(exc)))
                logger.exception(
                    "%s: order_id=%s failed this cycle (run_at=%s).",
                    AUTO_CANCEL_TASK_NAME,
                    order_id,
                    run_at,
                )
                continue

        # Persisted before the systemic-failure raise below, not after --
        # a cycle where every order failed is exactly the scenario most
        # worth a durable record.
        _persist_auto_cancel_audit_record(
            run_at=run_at,
            evaluated=evaluated,
            cancelled=cancelled,
            failed=failed,
            failures=failures,
        )

        if evaluated and failed == evaluated:
            raise RuntimeError(
                f"{AUTO_CANCEL_TASK_NAME}: every one of {evaluated} "
                f"evaluated orders failed this cycle (run_at="
                f"{run_at.isoformat()}) -- likely a systemic bug, not "
                "routine per-order skips. See preceding per-order log "
                "entries for the individual exceptions."
            )

        logger.info(
            "%s: run_at=%s evaluated=%s cancelled=%s failed=%s",
            AUTO_CANCEL_TASK_NAME,
            run_at,
            evaluated,
            cancelled,
            failed,
        )
        return {
            "run_at": run_at.isoformat(),
            "evaluated": evaluated,
            "cancelled": cancelled,
            "failed": failed,
        }
    finally:
        cache.delete(AUTO_CANCEL_LOCK_KEY)


# --- Receipt sweep -----------------------------------------------------------
#
# Task 62 follow-up (CodeRabbit, PR #93). The receipt email is queued after
# payment confirmation; if Redis was down at that moment, or the worker ran
# out of retries against Gmail, the receipt never went out and nothing tried
# again. This re-queues it.

RECEIPT_SWEEP_LOCK_KEY = "orders:receipt_sweep_lock"
RECEIPT_SWEEP_LOCK_TIMEOUT_SECONDS = 5 * 60
# Longer than the worker's own 1+2+4 minute retries plus a normal queue
# delay, so the sweep doesn't race a receipt that is still on its way.
RECEIPT_SWEEP_GRACE = timedelta(minutes=30)
# A receipt only matters for so long, and the cap bounds the query after a
# long outage. Anything that ages out unsent is logged by the sweeps that
# gave up on it (see below), never dropped silently.
RECEIPT_SWEEP_MAX_AGE = timedelta(days=7)
RECEIPT_SWEEP_BATCH_SIZE = 200


def _receipt_sweep_due(now):
    """Each re-queue waits twice as long as the last: 30, 60, then 120
    minutes after confirmation. So a receipt that keeps failing is retried a
    few times and then left alone, instead of every 15 minutes for days --
    and, being filtered out of the query, it never holds a batch slot that a
    newer missing receipt needs (design review finding)."""
    return reduce(
        or_,
        (
            Q(
                receipt_email_sweeps=sweeps,
                confirmed_at__lt=now - RECEIPT_SWEEP_GRACE * 2**sweeps,
            )
            for sweeps in range(RECEIPT_SWEEP_MAX_SWEEPS)
        ),
    )


@shared_task
def resend_missing_order_receipts():
    """Re-queues the receipt email for confirmed orders that still have not
    had one. Paid orders later cancelled or refunded are left alone."""
    if not cache.add(RECEIPT_SWEEP_LOCK_KEY, "1", RECEIPT_SWEEP_LOCK_TIMEOUT_SECONDS):
        logger.warning(
            "resend_missing_order_receipts: previous sweep still running; "
            "skipping this trigger."
        )
        return {"skipped": True}

    try:
        now = timezone.now()
        due = (
            Order.objects.filter(
                _receipt_sweep_due(now),
                receipt_email_sent_at__isnull=True,
                confirmed_at__gte=now - RECEIPT_SWEEP_MAX_AGE,
            )
            .exclude(email="")
            .exclude(status__in=RECEIPT_SKIP_STATUSES)
            .order_by("confirmed_at")
            .values_list("pk", "receipt_email_sweeps")[:RECEIPT_SWEEP_BATCH_SIZE]
        )

        requeued = 0
        for order_id, sweeps in list(due):
            # Conditional on the count read above, so an overlapping sweep
            # can't queue the same receipt twice.
            claimed = Order.objects.filter(
                pk=order_id, receipt_email_sweeps=sweeps
            ).update(receipt_email_sweeps=F("receipt_email_sweeps") + 1)
            if not claimed:
                continue
            try:
                send_order_receipt_email_task.delay(order_id)
            except Exception:
                # CodeRabbit (PR #93): nothing was queued, so this must not
                # use up one of the order's few attempts -- give it back, or
                # a flaky broker could exhaust them without a single send.
                # Conditional on the count this sweep wrote, so a change made
                # meanwhile (another sweep, or the job giving up) survives.
                # The broker can occasionally raise after accepting the
                # message; then the job runs AND the attempt is given back,
                # which costs one extra queued job at most -- the lock and
                # receipt_email_sent_at stop a second email.
                Order.objects.filter(
                    pk=order_id, receipt_email_sweeps=sweeps + 1
                ).update(receipt_email_sweeps=sweeps)
                logger.exception(
                    "resend_missing_order_receipts: could not queue the "
                    "receipt for order_id=%s; it stays due for the next sweep.",
                    order_id,
                )
                continue
            requeued += 1
            # Logged only once actually queued: a failed queue gives the
            # attempt back, so it would not have been the last one.
            if sweeps + 1 == RECEIPT_SWEEP_MAX_SWEEPS:
                logger.error(
                    "resend_missing_order_receipts: final attempt for "
                    "order_id=%s -- if this send fails too, the customer "
                    "will not get a receipt email automatically.",
                    order_id,
                )

        logger.info("resend_missing_order_receipts: requeued=%s", requeued)
        return {"requeued": requeued}
    finally:
        cache.delete(RECEIPT_SWEEP_LOCK_KEY)
