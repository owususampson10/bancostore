import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from celery import shared_task
from constance import config

from .models import Order, OrderCycleFailure, OrderCycleRun
from .services import _auto_cancel_pending_order

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


def _pending_order_ids_query(cutoff):
    """Ordered oldest-first, matching Withdrawal's own batch driver
    convention -- not a correctness requirement (this is one
    .iterator() snapshot processed once), but a deliberate, consistent
    fairness choice rather than leaving row order to whatever plan the
    database happens to pick."""
    return (
        Order.objects.filter(status=Order.Status.PENDING, created_at__lt=cutoff)
        .order_by("created_at")
        .values_list("pk", flat=True)
        .iterator()
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
