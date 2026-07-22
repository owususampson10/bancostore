import logging
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone

from celery import shared_task
from constance import config
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from apps.distributors.models import Distributor
from apps.pv_ledger.services import distributor_ids_with_pending_pv

from .models import BinaryBonusCycleFailure, BinaryBonusCycleRun
from .services import process_binary_bonus_for_distributor

logger = logging.getLogger(__name__)

TASK_NAME = "calculate-binary-bonus"
LOCK_KEY = "commissions:binary_bonus_cycle_lock"
# Deliberately NOT derived from the live (constance-synced) schedule interval
# -- this is a dead-worker self-heal budget, not a cycle-duration estimate.
# The loop below renews this TTL after every distributor it finishes
# processing, which covers the common case (many fast per-distributor
# calls whose CUMULATIVE time would otherwise exceed this timeout). It does
# NOT cover a single per-distributor call itself running longer than this
# timeout (a slow query, GC pause, or resource contention mid-call, as
# opposed to the bounded DB-lock-contention retries inside
# process_binary_bonus_for_distributor) -- renewal only happens between
# iterations, not during one (2026-07-22 security-and-hardening review).
# If that ever happens, a second Beat-triggered cycle CAN start with a
# different run_at while the first is still genuinely alive. This is a
# real gap in the "one run_at = one cycle" guarantee, not merely a
# theoretical one -- but it does not by itself cause a double-payment: any
# distributor both cycles touch is still serialized by
# process_binary_bonus_for_distributor's own row lock, and the weekly cap's
# re-read of committed WalletTransaction rows plus PV-bucket depletion both
# throttle whatever the second cycle would otherwise pay. Fund-safety in
# this edge case rests on those defenses, not on this lock alone.
LOCK_TIMEOUT_SECONDS = 9 * 60


def _sync_periodic_task_interval():
    """Best-effort: keeps the real Celery Beat schedule (a django_celery_beat
    PeriodicTask/IntervalSchedule pair, migration-seeded) in step with the
    admin-editable BINARY_BONUS_INTERVAL_MINUTES constance setting. Without
    this, that setting would be purely decorative -- constance and
    django_celery_beat are two independent DB-backed config stores that
    don't know about each other, and BINARY_BONUS_INTERVAL_MINUTES sits in
    the same admin fieldset as BINARY_BONUS_RATE/WEEKLY_BINARY_BONUS_CAP,
    which *are* live, so an admin has every reason to expect this one is
    too. django_celery_beat's DatabaseScheduler polls a change-timestamp
    every `beat_max_loop_interval` (default 5s, unset in this project) via
    PeriodicTasks.last_change(), so updating the real model here (not the
    migration's historical apps.get_model() version) takes effect within
    seconds, no beat restart needed.
    (Source: django_celery_beat/schedulers.py's DatabaseScheduler.schedule_changed
    and DEFAULT_MAX_INTERVAL, read from the installed package 2026-07-21.)

    Silently returns if no PeriodicTask row exists yet (e.g. local dev
    before the seeding migration ran) -- this is a convenience sync, not
    something the payout cycle itself should ever fail over.

    Only ever touches the `interval` schedule type. django_celery_beat's
    PeriodicTask supports four mutually-exclusive schedule types (interval/
    crontab/solar/clocked -- confirmed against the installed package's
    models.py). If an admin has repointed this task at one of the other
    three via django_celery_beat's own admin (e.g. a crontab restricting it
    to business hours), that's a deliberate choice made through a different,
    equally legitimate admin screen -- overwriting it back to an interval
    schedule every cycle would silently fight the admin. Skip and warn
    instead."""
    try:
        task = PeriodicTask.objects.select_related("interval").get(name=TASK_NAME)
    except PeriodicTask.DoesNotExist:
        return

    if task.crontab_id or task.solar_id or task.clocked_id:
        logger.warning(
            "calculate_binary_bonus: PeriodicTask %r is scheduled via a "
            "crontab/solar/clocked schedule, not an interval -- leaving it "
            "alone rather than overwriting it with BINARY_BONUS_INTERVAL_"
            "MINUTES.",
            TASK_NAME,
        )
        return

    desired_minutes = config.BINARY_BONUS_INTERVAL_MINUTES
    # Floor, not just a > 0 check -- BINARY_BONUS_INTERVAL_MINUTES has no
    # CONSTANCE_ADDITIONAL_FIELDS bounds, so a fat-fingered "1" is one admin
    # form submission away. This task shares the default Celery queue/worker
    # pool with everything else in this project (no CELERY_TASK_ROUTES), so
    # an unbounded-low interval is a real, admin-reachable DoS vector on
    # shared infrastructure (2026-07-22 security-and-hardening review), not
    # just a self-inflicted inefficiency.
    MIN_INTERVAL_MINUTES = 5
    if desired_minutes < MIN_INTERVAL_MINUTES:
        logger.warning(
            "calculate_binary_bonus: BINARY_BONUS_INTERVAL_MINUTES=%s is "
            "below the enforced floor of %s minutes -- leaving the current "
            "schedule (every=%s) in place rather than syncing to it.",
            desired_minutes,
            MIN_INTERVAL_MINUTES,
            task.interval.every if task.interval else None,
        )
        return

    current = task.interval
    if (
        current is not None
        and current.every == desired_minutes
        and current.period == IntervalSchedule.MINUTES
    ):
        return

    # get_or_create, not an in-place update of `current` -- an IntervalSchedule
    # row can be shared by multiple PeriodicTasks (django_celery_beat dedupes
    # by (every, period)), so mutating it in place could silently reschedule
    # an unrelated task that happens to already share this exact interval.
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=desired_minutes, period=IntervalSchedule.MINUTES
    )
    task.interval = schedule
    task.save(update_fields=["interval"])


@shared_task
def calculate_binary_bonus():
    """Batch driver for Task 13's Binary Bonus: runs one payout cycle for
    every distributor with outstanding PV. The per-distributor money math
    lives entirely in process_binary_bonus_for_distributor -- this function
    only owns cycle-level concerns: generating and holding a single fixed
    run_at, picking which distributors to evaluate, and isolating one
    distributor's failure from the rest of the batch.

    Overlap protection: a Redis SET-NX (django-redis's cache.add(), atomic
    on this project's configured backend -- confirmed against django-redis's
    client/default.py source, which passes nx=True straight through to
    redis-py's SET command) guards against two live cycles running at once.
    Two overlapping cycles wouldn't double-pay outright (the weekly cap
    re-reads committed WalletTransaction rows before crediting), but they
    would each mint their own run_at, breaking the "one run_at = one
    logical cycle" invariant process_binary_bonus_for_distributor's
    idempotency and audit story rests on -- worth avoiding outright rather
    than merely tolerating, especially as distributor count grows toward
    this platform's stated long-term scale.

    If every evaluated distributor fails, raises rather than returning a
    quiet all-zero summary -- a systemic bug (bad deploy, missing
    migration) must show up as a failed Celery task, not look identical to
    "quiet cycle, nothing owed" to Flower/monitoring, since this job runs
    unattended with no human review per cycle."""
    if not cache.add(LOCK_KEY, "1", LOCK_TIMEOUT_SECONDS):
        logger.warning(
            "calculate_binary_bonus: previous cycle still in progress "
            "(lock held) -- skipping this trigger rather than running "
            "a second concurrent cycle."
        )
        return {"skipped": True, "reason": "previous cycle still in progress"}

    try:
        try:
            _sync_periodic_task_interval()
        except Exception:
            logger.exception(
                "calculate_binary_bonus: failed to sync the PeriodicTask "
                "interval from BINARY_BONUS_INTERVAL_MINUTES -- continuing "
                "with this cycle's payouts regardless, since a stale "
                "schedule is a lesser problem than skipping payouts."
            )

        run_at = timezone.now()
        evaluated = 0
        paid = 0
        failed = 0
        total_amount = Decimal("0.00")
        failures = []  # [(distributor_id, error_message), ...] -- real
        # exceptions only, not routine zero-payout skips (see
        # BinaryBonusCycleFailure's docstring for why that distinction
        # matters at this platform's scale).

        for distributor_id in distributor_ids_with_pending_pv().iterator():
            evaluated += 1
            # Renews the lock's TTL on every iteration, not just at
            # acquisition -- without this, a batch that's still alive but
            # runs past LOCK_TIMEOUT_SECONDS would lose the lock mid-cycle
            # and let a second Beat trigger start a genuinely concurrent
            # cycle with a different run_at. A worker that crashes mid-loop
            # simply stops renewing, so the lock still self-heals via its
            # own TTL exactly as before.
            cache.set(LOCK_KEY, "1", LOCK_TIMEOUT_SECONDS)
            try:
                amount = process_binary_bonus_for_distributor(
                    Distributor(pk=distributor_id), run_at
                )
            except Exception as exc:
                failed += 1
                failures.append((distributor_id, str(exc)))
                logger.exception(
                    "calculate_binary_bonus: distributor_id=%s failed this "
                    "cycle (run_at=%s).",
                    distributor_id,
                    run_at,
                )
                continue

            if amount > 0:
                paid += 1
                total_amount += amount

        # Persisted before the systemic-failure raise below, not after --
        # a cycle where every distributor failed is exactly the scenario
        # most worth a durable record, since the Celery task result itself
        # will show only FAILURE with no further detail. Best-effort: a
        # bug in this bookkeeping must never be the reason this cycle's
        # actual payouts (already committed above, per distributor) get
        # lost from the raise/return path below.
        try:
            cycle_run = BinaryBonusCycleRun.objects.create(
                run_at=run_at,
                evaluated=evaluated,
                paid=paid,
                failed=failed,
                total_amount=total_amount,
            )
            if failures:
                BinaryBonusCycleFailure.objects.bulk_create(
                    BinaryBonusCycleFailure(
                        cycle_run=cycle_run, distributor_id=did, error=err
                    )
                    for did, err in failures
                )
        except Exception:
            # Falls back to exactly the ephemeral-logging gap this audit
            # trail exists to close (code-review note, 2026-07-22) -- an
            # acceptable last resort, not a fixed gap, since this branch
            # should be rare (the same DB this writes to already just took
            # every payout write for this cycle).
            logger.exception(
                "calculate_binary_bonus: failed to persist the audit-trail "
                "record for run_at=%s -- continuing regardless, since a "
                "missing audit row is a lesser problem than losing the "
                "raise/return below.",
                run_at,
            )

        if evaluated and failed == evaluated:
            raise RuntimeError(
                f"calculate_binary_bonus: every one of {evaluated} "
                f"evaluated distributors failed this cycle (run_at="
                f"{run_at.isoformat()}) -- likely a systemic bug, not "
                "routine per-distributor skips. See preceding per-"
                "distributor log entries for the individual exceptions."
            )

        logger.info(
            "calculate_binary_bonus: run_at=%s evaluated=%s paid=%s "
            "failed=%s total_amount=%s",
            run_at,
            evaluated,
            paid,
            failed,
            total_amount,
        )
        return {
            "run_at": run_at.isoformat(),
            "evaluated": evaluated,
            "paid": paid,
            "failed": failed,
            "total_amount": str(total_amount),
        }
    finally:
        cache.delete(LOCK_KEY)
