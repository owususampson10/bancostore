import logging
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from celery import shared_task
from constance import config

from apps.distributors.models import Distributor
from apps.pv_ledger.services import (
    distributor_ids_with_pending_pv,
    get_carry_forward_summary,
)

from .models import (
    Notification,
    NotificationCycleFailure,
    NotificationCycleRun,
    NotificationTemplate,
)
from .rendering import render_or_default
from .services import send_notification

logger = logging.getLogger(__name__)

PV_EXPIRY_TASK_NAME = "send-pv-expiry-notifications"
PV_EXPIRY_LOCK_KEY = "notifications:pv_expiry_cycle_lock"
# Per-distributor work here is one read-only get_carry_forward_summary call
# (3 queries) plus, rarely, one send_notification -- much cheaper than
# Withdrawal's per-row Paystack-call budget. Still renewed every iteration,
# matching every other batch driver's convention rather than a bespoke
# one-off number.
PV_EXPIRY_LOCK_TIMEOUT_SECONDS = 5 * 60


def _notify_if_pv_nearing_expiry(distributor_id, run_at) -> bool:
    """Returns True if a notification was sent this cycle, False if the
    distributor is skipped (not actually nearing expiry as of this exact
    moment -- the candidate query is a coarse pre-filter, not the
    authoritative check -- or already notified within the current
    warning window)."""
    try:
        distributor = Distributor.objects.get(pk=distributor_id)
    except Distributor.DoesNotExist:
        return False

    summary = get_carry_forward_summary(distributor, now=run_at)
    if not summary.nearing_expiry:
        return False

    # Dedup: without this, a distributor whose nearest-expiring batch
    # sits inside the warning window for several consecutive scheduled
    # runs would get a duplicate notification every single run until it
    # actually expires or is consumed. The warning window itself is
    # exactly PV_EXPIRY_WARNING_DAYS days long by definition (that's when
    # nearing_expiry starts being True), so "already notified within the
    # last PV_EXPIRY_WARNING_DAYS days" is exactly one notification per
    # distinct nearing-expiry episode -- no separate field needed to
    # track "have we warned about this specific batch yet."
    already_notified = Notification.objects.filter(
        distributor_id=distributor_id,
        event_type=Notification.EventType.PV_EXPIRING,
        created_at__gte=run_at - timedelta(days=config.PV_EXPIRY_WARNING_DAYS),
    ).exists()
    if already_notified:
        return False

    send_notification(
        distributor,
        Notification.EventType.PV_EXPIRING,
        render_or_default(
            NotificationTemplate.Key.PV_EXPIRING,
            {"pv": summary.pv, "expiry_date": summary.nearest_expiry_date},
            default_body=(
                "You have {{pv}} PV expiring around {{expiry_date}} -- use "
                "it before it's lost!"
            ),
        ),
    )
    return True


def _persist_pv_expiry_audit_record(*, run_at, evaluated, notified, failed, failures):
    """Persisted before the systemic-failure raise below, not after --
    mirrors apps.orders.tasks._persist_auto_cancel_audit_record's own
    identical reasoning and best-effort fallback."""
    try:
        cycle_run = NotificationCycleRun.objects.create(
            run_at=run_at, evaluated=evaluated, notified=notified, failed=failed
        )
        if failures:
            NotificationCycleFailure.objects.bulk_create(
                NotificationCycleFailure(
                    cycle_run=cycle_run, distributor_id=distributor_id, error=error
                )
                for distributor_id, error in failures
            )
    except Exception:
        logger.exception(
            "%s: failed to persist the audit-trail record for run_at=%s -- "
            "continuing regardless, since a missing audit row is a lesser "
            "problem than losing the raise/return that follows.",
            PV_EXPIRY_TASK_NAME,
            run_at,
        )


@shared_task
def send_pv_expiry_notifications():
    """Task 21d-iii: Section 6.6's 6th and final event type ("a PV batch
    is approaching expiry") -- the only scheduled one, unlike the other
    5 (immediate, event-driven -- Task 21d-ii). Shape mirrors
    apps.orders.tasks.auto_cancel_unpaid_orders exactly (fixed run_at,
    per-iteration-renewed lock, per-distributor exception isolation,
    systemic-failure guard, audit record persisted before that guard's
    raise) but is its own hand-written driver rather than a call into
    apps.commissions.tasks._run_commission_cycle -- that helper's
    process_one contract returns a Decimal amount earned, which doesn't
    fit a job that credits nothing.

    Reuses apps.pv_ledger.services.get_carry_forward_summary (Task 21b)
    for the actual "is this distributor's PV nearing expiry, how much,
    and when" logic rather than re-deriving it -- the dashboard's own
    Carry-Forward PV card and this notification must never be able to
    disagree about whose PV is nearing expiry.

    Candidate distributors come from
    apps.pv_ledger.services.distributor_ids_with_pending_pv -- every
    distributor with ANY non-zero PvDailyBucket row, not just those
    nearing expiry (get_carry_forward_summary itself is the precise
    per-candidate check) -- so this never scans every registered user,
    matching SPEC.md's Scale Architecture the same way every other
    batch driver in this codebase already does.

    No PeriodicTask interval self-sync (unlike Binary/Matching Bonus),
    matching auto_cancel_unpaid_orders's own reasoning exactly:
    PV_EXPIRY_WARNING_DAYS is the warning-window *threshold* this task
    reads every run, not this task's own *run frequency* -- there is no
    second, separate "how often does this job run" constance setting to
    keep in sync."""
    if not cache.add(PV_EXPIRY_LOCK_KEY, "1", PV_EXPIRY_LOCK_TIMEOUT_SECONDS):
        logger.warning(
            "%s: previous cycle still in progress (lock held) -- skipping "
            "this trigger rather than running a second concurrent cycle.",
            PV_EXPIRY_TASK_NAME,
        )
        return {"skipped": True, "reason": "previous cycle still in progress"}

    try:
        run_at = timezone.now()
        evaluated = 0
        notified = 0
        failed = 0
        failures = []  # [(distributor_id, error_message), ...]

        for distributor_id in distributor_ids_with_pending_pv():
            evaluated += 1
            cache.set(PV_EXPIRY_LOCK_KEY, "1", PV_EXPIRY_LOCK_TIMEOUT_SECONDS)
            try:
                if _notify_if_pv_nearing_expiry(distributor_id, run_at):
                    notified += 1
            except Exception as exc:
                failed += 1
                failures.append((distributor_id, str(exc)))
                logger.exception(
                    "%s: distributor_id=%s failed this cycle (run_at=%s).",
                    PV_EXPIRY_TASK_NAME,
                    distributor_id,
                    run_at,
                )
                continue

        _persist_pv_expiry_audit_record(
            run_at=run_at,
            evaluated=evaluated,
            notified=notified,
            failed=failed,
            failures=failures,
        )

        if evaluated and failed == evaluated:
            raise RuntimeError(
                f"{PV_EXPIRY_TASK_NAME}: every one of {evaluated} evaluated "
                f"distributors failed this cycle (run_at={run_at.isoformat()}) "
                "-- likely a systemic bug, not routine per-distributor skips. "
                "See preceding per-distributor log entries for the "
                "individual exceptions."
            )

        logger.info(
            "%s: run_at=%s evaluated=%s notified=%s failed=%s",
            PV_EXPIRY_TASK_NAME,
            run_at,
            evaluated,
            notified,
            failed,
        )
        return {
            "run_at": run_at.isoformat(),
            "evaluated": evaluated,
            "notified": notified,
            "failed": failed,
        }
    finally:
        cache.delete(PV_EXPIRY_LOCK_KEY)
