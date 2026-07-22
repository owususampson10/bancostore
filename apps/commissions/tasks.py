import logging
from decimal import Decimal

from django.core.cache import cache
from django.utils import timezone

from celery import shared_task
from constance import config
from django_celery_beat.models import IntervalSchedule, PeriodicTask

from apps.distributors.models import Distributor
from apps.pv_ledger.services import distributor_ids_with_pending_pv

from .models import CommissionCycleFailure, CommissionCycleRun
from .services import (
    distributor_ids_eligible_for_matching_bonus,
    process_binary_bonus_for_distributor,
    process_matching_bonus_for_distributor,
)

logger = logging.getLogger(__name__)

BINARY_BONUS_TASK_NAME = "calculate-binary-bonus"
BINARY_BONUS_LOCK_KEY = "commissions:binary_bonus_cycle_lock"
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
BINARY_BONUS_LOCK_TIMEOUT_SECONDS = 9 * 60

MATCHING_BONUS_TASK_NAME = "calculate-matching-bonus"
MATCHING_BONUS_LOCK_KEY = "commissions:matching_bonus_cycle_lock"
# Deliberately much larger than Binary Bonus's, not copied -- re-derived.
# Silver-rank distributors have genuinely unlimited sponsor-chain depth
# (apps.commissions.services.sum_downline_binary_bonus_earnings), unlike
# Binary Bonus's per-distributor cost, which is proven O(1)/O(log n) at
# 16k+ tree nodes (Task 9/10). The NUMBER of bulk queries the sponsor-chain
# walk issues is bounded by depth alone (one per level, hard-capped at
# MAX_MATCHING_BONUS_WALK_DEPTH regardless of config) -- but each level's
# result size, and the final aggregate query's IN-clause, both still scale
# with downline SIZE, not just depth (code-review correction, 2026-07-22:
# an earlier version of this comment overstated what's actually bounded).
# There's no at-scale proof for either axis the way there is for the
# binary tree. The 7-day (floor 1-day) cadence gives ample headroom to be
# generous here rather than guess a tight number (2026-07-22 doubt-driven-
# development review, Task 14).
MATCHING_BONUS_LOCK_TIMEOUT_SECONDS = 30 * 60


def _sync_periodic_task_interval(
    *, task_name, setting_name, desired_value, period, min_value
):
    """Best-effort: keeps a real Celery Beat schedule (a django_celery_beat
    PeriodicTask/IntervalSchedule pair, migration-seeded) in step with an
    admin-editable constance interval setting. Without this, that setting
    would be purely decorative -- constance and django_celery_beat are two
    independent DB-backed config stores that don't know about each other,
    and these interval settings sit in the same admin fieldset as rates/
    caps that ARE live, so an admin has every reason to expect this one is
    too. django_celery_beat's DatabaseScheduler polls a change-timestamp
    every `beat_max_loop_interval` (default 5s, unset in this project) via
    PeriodicTasks.last_change(), so updating the real model here (not a
    migration's historical apps.get_model() version) takes effect within
    seconds, no beat restart needed.
    (Source: django_celery_beat/schedulers.py's DatabaseScheduler.schedule_changed
    and DEFAULT_MAX_INTERVAL, read from the installed package 2026-07-21.)

    Generic across every commission task in this module (extracted
    2026-07-22 once Task 14's Matching Bonus needed the exact same shape
    Task 13's Binary Bonus already had, differing only in which task,
    which constance key, which schedule period unit, and which floor).

    Silently returns if no PeriodicTask row exists yet (e.g. local dev
    before the seeding migration ran) -- this is a convenience sync, not
    something the payout cycle itself should ever fail over.

    Only ever touches the `interval` schedule type. django_celery_beat's
    PeriodicTask supports four mutually-exclusive schedule types (interval/
    crontab/solar/clocked -- confirmed against the installed package's
    models.py). If an admin has repointed a task at one of the other three
    via django_celery_beat's own admin (e.g. a crontab restricting it to
    business hours), that's a deliberate choice made through a different,
    equally legitimate admin screen -- overwriting it back to an interval
    schedule every cycle would silently fight the admin. Skip and warn
    instead."""
    try:
        task = PeriodicTask.objects.select_related("interval").get(name=task_name)
    except PeriodicTask.DoesNotExist:
        return

    if task.crontab_id or task.solar_id or task.clocked_id:
        logger.warning(
            "%s: PeriodicTask %r is scheduled via a crontab/solar/clocked "
            "schedule, not an interval -- leaving it alone rather than "
            "overwriting it with %s.",
            task_name,
            task_name,
            setting_name,
        )
        return

    # Floor, not just a > 0 check -- these interval settings have no
    # CONSTANCE_ADDITIONAL_FIELDS bounds beyond min_value (a fat-fingered
    # tiny value is one admin form submission away). These tasks share the
    # default Celery queue/worker pool with everything else in this
    # project (no CELERY_TASK_ROUTES), so an unbounded-low interval is a
    # real, admin-reachable DoS vector on shared infrastructure
    # (2026-07-22 security-and-hardening review), not just a
    # self-inflicted inefficiency.
    if desired_value < min_value:
        logger.warning(
            "%s: %s=%s is below the enforced floor of %s -- leaving the "
            "current schedule (every=%s) in place rather than syncing to "
            "it.",
            task_name,
            setting_name,
            desired_value,
            min_value,
            task.interval.every if task.interval else None,
        )
        return

    current = task.interval
    if (
        current is not None
        and current.every == desired_value
        and current.period == period
    ):
        return

    # get_or_create, not an in-place update of `current` -- an IntervalSchedule
    # row can be shared by multiple PeriodicTasks (django_celery_beat dedupes
    # by (every, period)), so mutating it in place could silently reschedule
    # an unrelated task that happens to already share this exact interval.
    schedule, _ = IntervalSchedule.objects.get_or_create(
        every=desired_value, period=period
    )
    task.interval = schedule
    task.save(update_fields=["interval"])


def _persist_cycle_audit_record(
    *, job_name, run_at, evaluated, paid, failed, total_amount, failures
):
    """Persists a CommissionCycleRun (+ one CommissionCycleFailure per
    real per-distributor exception) for one completed cycle -- shared by
    every commission task, called before that task's own systemic-failure
    raise, not after, so the one cycle most worth an audit record (every
    distributor failed) doesn't lose it along with the exception.

    Best-effort: falls back to exactly the ephemeral-logging gap this
    audit trail exists to close (code-review note, 2026-07-22) if the
    write itself fails -- an acceptable last resort, not a fixed gap,
    since this branch should be rare (the same DB this writes to already
    just took every payout write for this cycle)."""
    try:
        cycle_run = CommissionCycleRun.objects.create(
            job_name=job_name,
            run_at=run_at,
            evaluated=evaluated,
            paid=paid,
            failed=failed,
            total_amount=total_amount,
        )
        if failures:
            CommissionCycleFailure.objects.bulk_create(
                CommissionCycleFailure(
                    cycle_run=cycle_run, distributor_id=did, error=err
                )
                for did, err in failures
            )
    except Exception:
        logger.exception(
            "%s: failed to persist the audit-trail record for run_at=%s -- "
            "continuing regardless, since a missing audit row is a lesser "
            "problem than losing the raise/return that follows.",
            job_name,
            run_at,
        )


def _run_commission_cycle(
    *, job_name, lock_key, lock_timeout_seconds, sync_interval, ids_query, process_one
):
    """Generic Celery Beat batch-cycle driver shared by every commission
    task in this module -- extracted 2026-07-22 once Task 14's Matching
    Bonus needed the exact shape Task 13's Binary Bonus already had: a
    single fixed run_at held across the whole cycle (the sole idempotency
    key each per-distributor function relies on), a per-iteration-renewed
    Redis lock preventing overlapping cycles, per-distributor exception
    isolation, a systemic-failure guard, and a durable audit-trail record
    persisted before that guard's raise.

    `sync_interval` / `ids_query` / `process_one` are each task's own
    zero-or-one-arg closures over its specific constance settings and
    per-distributor function -- this function owns only the cycle-level
    orchestration, never anything bonus-type-specific."""
    if not cache.add(lock_key, "1", lock_timeout_seconds):
        logger.warning(
            "%s: previous cycle still in progress (lock held) -- skipping "
            "this trigger rather than running a second concurrent cycle.",
            job_name,
        )
        return {"skipped": True, "reason": "previous cycle still in progress"}

    try:
        try:
            sync_interval()
        except Exception:
            logger.exception(
                "%s: failed to sync the PeriodicTask interval -- continuing "
                "with this cycle's payouts regardless, since a stale "
                "schedule is a lesser problem than skipping payouts.",
                job_name,
            )

        run_at = timezone.now()
        evaluated = 0
        paid = 0
        failed = 0
        total_amount = Decimal("0.00")
        failures = []  # [(distributor_id, error_message), ...] -- real
        # exceptions only, not routine zero-payout skips (see
        # CommissionCycleFailure's docstring for why that distinction
        # matters at this platform's scale).

        for distributor_id in ids_query():
            evaluated += 1
            # Renews the lock's TTL on every iteration, not just at
            # acquisition -- without this, a batch that's still alive but
            # runs past lock_timeout_seconds would lose the lock mid-cycle
            # and let a second Beat trigger start a genuinely concurrent
            # cycle with a different run_at. A worker that crashes mid-loop
            # simply stops renewing, so the lock still self-heals via its
            # own TTL exactly as before.
            cache.set(lock_key, "1", lock_timeout_seconds)
            try:
                amount = process_one(Distributor(pk=distributor_id), run_at)
            except Exception as exc:
                failed += 1
                failures.append((distributor_id, str(exc)))
                logger.exception(
                    "%s: distributor_id=%s failed this cycle (run_at=%s).",
                    job_name,
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
        # will show only FAILURE with no further detail.
        _persist_cycle_audit_record(
            job_name=job_name,
            run_at=run_at,
            evaluated=evaluated,
            paid=paid,
            failed=failed,
            total_amount=total_amount,
            failures=failures,
        )

        if evaluated and failed == evaluated:
            raise RuntimeError(
                f"{job_name}: every one of {evaluated} evaluated "
                f"distributors failed this cycle (run_at="
                f"{run_at.isoformat()}) -- likely a systemic bug, not "
                "routine per-distributor skips. See preceding per-"
                "distributor log entries for the individual exceptions."
            )

        logger.info(
            "%s: run_at=%s evaluated=%s paid=%s failed=%s total_amount=%s",
            job_name,
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
        cache.delete(lock_key)


def _sync_binary_bonus_interval():
    _sync_periodic_task_interval(
        task_name=BINARY_BONUS_TASK_NAME,
        setting_name="BINARY_BONUS_INTERVAL_MINUTES",
        desired_value=config.BINARY_BONUS_INTERVAL_MINUTES,
        period=IntervalSchedule.MINUTES,
        min_value=5,
    )


def _sync_matching_bonus_interval():
    _sync_periodic_task_interval(
        task_name=MATCHING_BONUS_TASK_NAME,
        setting_name="MATCHING_BONUS_INTERVAL_DAYS",
        desired_value=config.MATCHING_BONUS_INTERVAL_DAYS,
        period=IntervalSchedule.DAYS,
        min_value=1,
    )


@shared_task
def calculate_binary_bonus():
    """Batch driver for Task 13's Binary Bonus: runs one payout cycle for
    every distributor with outstanding PV. The per-distributor money math
    lives entirely in process_binary_bonus_for_distributor; the cycle-level
    concerns (fixed run_at, overlap lock, exception isolation, audit trail,
    systemic-failure guard) live in the shared _run_commission_cycle.

    Two overlapping cycles wouldn't double-pay outright even if the lock
    were ever bypassed (the weekly cap re-reads committed WalletTransaction
    rows before crediting), but they would each mint their own run_at,
    breaking the "one run_at = one logical cycle" invariant
    process_binary_bonus_for_distributor's idempotency and audit story
    rests on -- worth avoiding outright rather than merely tolerating."""
    return _run_commission_cycle(
        job_name=BINARY_BONUS_TASK_NAME,
        lock_key=BINARY_BONUS_LOCK_KEY,
        lock_timeout_seconds=BINARY_BONUS_LOCK_TIMEOUT_SECONDS,
        sync_interval=_sync_binary_bonus_interval,
        ids_query=lambda: distributor_ids_with_pending_pv().iterator(),
        process_one=process_binary_bonus_for_distributor,
    )


@shared_task
def calculate_matching_bonus():
    """Batch driver for Task 14's Matching Bonus: runs one payout cycle
    for every distributor with a matching-bonus-eligible rank and at
    least one direct referral. The per-distributor logic (eligibility,
    rank-derived depth, sponsor-chain BFS, calculation, credit) lives
    entirely in process_matching_bonus_for_distributor; cycle-level
    concerns are the same shared _run_commission_cycle Binary Bonus uses.

    Unlike Binary Bonus, this never mutates another distributor's data --
    it only reads downline members' already-committed WalletTransaction
    rows. That does NOT make overlapping cycles double-pay-safe, though
    (CodeRabbit review, 2026-07-22, correcting an earlier version of this
    docstring that conflated the two): the idempotency `reference` is
    `matching-bonus-{pk}-{run_at}`, so two overlapping cycles mint two
    distinct `run_at` values, two distinct references, and both pass the
    `existing is None` check -- crediting the same distributor twice for
    what's mostly the same underlying downline earnings. Binary Bonus
    survives a bypassed lock because apply_weekly_binary_bonus_cap
    re-reads committed WalletTransaction rows before crediting, a real
    second line of defense; process_matching_bonus_for_distributor has no
    such cap (this module's docstring: "no weekly cap ... not omitted by
    oversight"). This lock is therefore the SOLE protection against a
    double-pay here, not a belt-and-suspenders backstop the way it is for
    Binary Bonus -- do not relax it on the assumption this task is
    inherently safer."""
    return _run_commission_cycle(
        job_name=MATCHING_BONUS_TASK_NAME,
        lock_key=MATCHING_BONUS_LOCK_KEY,
        lock_timeout_seconds=MATCHING_BONUS_LOCK_TIMEOUT_SECONDS,
        sync_interval=_sync_matching_bonus_interval,
        ids_query=lambda: distributor_ids_eligible_for_matching_bonus().iterator(),
        process_one=process_matching_bonus_for_distributor,
    )
