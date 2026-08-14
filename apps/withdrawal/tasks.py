import logging
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, Sum
from django.utils import timezone

from celery import shared_task
from constance import config
from django_celery_beat.models import CrontabSchedule, PeriodicTask

from apps.distributors.paystack import (
    REQUEST_TIMEOUT_SECONDS,
    PaystackError,
    verify_transfer,
)

from .models import WithdrawalCycleFailure, WithdrawalCycleRun, WithdrawalRequest
from .services import (
    WithdrawalRequestNotFound,
    apply_verified_transfer_outcome,
    process_withdrawal_payout,
)

logger = logging.getLogger(__name__)

WITHDRAWAL_PAYOUT_TASK_NAME = "process-withdrawal-payouts"
WITHDRAWAL_PAYOUT_LOCK_KEY = "withdrawal:payout_cycle_lock"

# Task 16f PR 2 (doubt-driven-development finding, 2026-07-24): NOT copied
# from Binary Bonus's 9-minute value. That value was sized for a cycle of
# many fast, pure-DB per-distributor calls; process_withdrawal_payout can
# make up to 3 sequential Paystack network calls per row (create_transfer_
# recipient, verify_transfer, and/or initiate_transfer), each bounded by
# REQUEST_TIMEOUT_SECONDS. This lock is renewed once per iteration (see
# the loop below), so it only ever needs to cover ONE row's worst-case
# duration, not the whole cycle -- the 300s floor is what's actually in
# effect at today's REQUEST_TIMEOUT_SECONDS=10 (3*10*4=120 < 300); the
# formula exists so this scales automatically if REQUEST_TIMEOUT_SECONDS
# ever grows, rather than silently staying pinned at a value sized for
# today's timeout.
#
# code-review-and-quality (2026-07-24, Task 16g): a row's worst case now
# also includes one SMS send (apps.withdrawal.services._notify, called
# from apply_verified_transfer_outcome once a terminal outcome is
# reached) -- mNotify's own request has a 10s timeout
# (apps.notifications.sms._send_via_mnotify). Still comfortably inside
# the 300s floor (worst case ~40s: 3 Paystack calls + 1 SMS call), so the
# margin holds, but this is a 4th call this budget now needs to account
# for, not just the 3 Paystack ones described above.
#
# This is not the only defense against two overlapping cycles both
# calling initiate_transfer for the same row if this timeout is ever
# exceeded anyway: claim_for_payout's own docstring notes that Paystack's
# reference uniqueness is meant to reject a second initiate_transfer call
# for the same pinned reference. That's an unverified third-party
# guarantee on this account (Paystack Transfers are still blocked on this
# sandbox's tier -- see the project_paystack_transfer_account_tier_blocked
# memory), so it is a second layer, not a substitute for sizing this
# timeout deliberately -- mirroring Matching Bonus's own honest framing of
# exactly what protects a cycle against overlap, rather than assuming a
# copied number is automatically safe here too.
WITHDRAWAL_PAYOUT_LOCK_TIMEOUT_SECONDS = max(3 * REQUEST_TIMEOUT_SECONDS * 4, 300)

# django_celery_beat's CrontabSchedule.day_of_week is plain crontab syntax
# (confirmed against the installed package, 2026-07-24: 0 or 7 = Sunday,
# 1 = Monday, ... 6 = Saturday) -- it does NOT accept full weekday names.
# WITHDRAWAL_DAY's seeded constance value is the lowercase string "friday"
# (apps/platform_settings/config.py), so it must always be converted
# through this explicit table before being written to a real
# CrontabSchedule row -- not celery's own internal name-abbreviation
# parser, so the mapping stays visible and directly testable here.
_WEEKDAY_TO_CRONTAB_DAY_OF_WEEK = {
    "sunday": "0",
    "monday": "1",
    "tuesday": "2",
    "wednesday": "3",
    "thursday": "4",
    "friday": "5",
    "saturday": "6",
}


def _withdrawal_payout_ids_query():
    """approved_debited rows are ready for their first payout attempt;
    queued_for_payout rows are ones a prior cycle claimed (pinned a
    reference) but didn't resolve to a terminal outcome -- both need
    revisiting every cycle. Excluding queued_for_payout here would make
    a claimed-but-unresolved row unreachable forever (the exact
    "resume path unreachable" bug 16f's original doubt-driven-development
    pass caught in the design, before any code existed).

    Ordered oldest-submitted-first (doubt-driven-development finding,
    2026-07-24) -- not a correctness requirement (this is one .iterator()
    snapshot processed once, not paginated across separate queries the
    way tests/feature/distributors/test_earnings_history.py's bug was),
    but a deliberate fairness choice for a real-money weekly batch rather
    than leaving row order to whatever plan MySQL happens to pick."""
    return (
        WithdrawalRequest.objects.filter(
            status__in=[
                WithdrawalRequest.Status.APPROVED_DEBITED,
                WithdrawalRequest.Status.QUEUED_FOR_PAYOUT,
            ]
        )
        .order_by("created_at")
        .values_list("pk", flat=True)
        .iterator()
    )


def _sync_withdrawal_day_crontab():
    """Keeps the real Celery Beat CrontabSchedule in step with the
    admin-editable WITHDRAWAL_DAY constance setting -- mirrors
    bancostore.celery_beat.sync_periodic_task_interval's own reasoning
    (a live-editable setting sitting in the same admin fieldset as other
    settings that already take effect live must not be silently
    decorative), but for a crontab schedule, not an interval one -- no
    prior CrontabSchedule precedent exists in this codebase.

    Silently returns if no PeriodicTask row exists yet (e.g. local dev
    before the seeding migration ran), matching the interval-sync
    convention exactly.

    Validates the converted value BEFORE writing it (doubt-driven-
    development finding, 2026-07-24): confirmed directly against the
    installed django_celery_beat package that day_of_week_validator
    rejects "friday" outright, and that Django model validators never
    run automatically on .save()/get_or_create() -- only via
    full_clean(). Writing an unvalidated value here would either persist
    a broken schedule silently, or fail confusingly deep inside Celery
    Beat's own scheduling loop, which is shared with every other
    PeriodicTask in this project, not isolated to this one. On anything
    unrecognized or invalid, leaves the existing schedule alone and
    logs a warning -- the same defensive fallback
    _sync_periodic_task_interval uses for an out-of-bounds interval."""
    try:
        task = PeriodicTask.objects.select_related("crontab").get(
            name=WITHDRAWAL_PAYOUT_TASK_NAME
        )
    except PeriodicTask.DoesNotExist:
        return

    if task.interval_id or task.solar_id or task.clocked_id:
        logger.warning(
            "%s: PeriodicTask %r is scheduled via an interval/solar/clocked "
            "schedule, not a crontab -- leaving it alone rather than "
            "overwriting it with WITHDRAWAL_DAY.",
            WITHDRAWAL_PAYOUT_TASK_NAME,
            WITHDRAWAL_PAYOUT_TASK_NAME,
        )
        return

    desired_day = str(config.WITHDRAWAL_DAY).strip().lower()
    try:
        day_of_week = _WEEKDAY_TO_CRONTAB_DAY_OF_WEEK[desired_day]
    except KeyError:
        logger.warning(
            "%s: WITHDRAWAL_DAY=%r is not a recognized weekday -- leaving "
            "the current schedule in place rather than syncing to it.",
            WITHDRAWAL_PAYOUT_TASK_NAME,
            config.WITHDRAWAL_DAY,
        )
        return

    current = task.crontab
    if (
        current is not None
        and current.day_of_week == day_of_week
        and current.minute == "0"
        and current.hour == "0"
        and current.day_of_month == "*"
        and current.month_of_year == "*"
    ):
        return

    # get_or_create, not an in-place update -- a CrontabSchedule row can be
    # shared by multiple PeriodicTasks (django_celery_beat dedupes by
    # field values), so mutating it in place could silently reschedule an
    # unrelated task that happens to already share this exact schedule.
    schedule, _ = CrontabSchedule.objects.get_or_create(
        minute="0",
        hour="0",
        day_of_month="*",
        month_of_year="*",
        day_of_week=day_of_week,
    )
    task.crontab = schedule
    task.save(update_fields=["crontab"])


def _snapshot_for_failure_record(withdrawal_request_id):
    """Best-effort read of distributor_id/net_amount from the still-live
    row at failure time (doubt-driven-development finding, 2026-07-24) --
    not from process_withdrawal_payout's own return value, which never
    reached the caller on this path. Falls back to (None, None) if even
    this read fails (e.g. the row itself vanished) rather than losing the
    failure record entirely -- WithdrawalCycleFailure's own both-or-
    neither constraint permits exactly that shape."""
    try:
        row = WithdrawalRequest.objects.values("distributor_id", "net_amount").get(
            pk=withdrawal_request_id
        )
        return row["distributor_id"], row["net_amount"]
    except WithdrawalRequest.DoesNotExist:
        return None, None


def _persist_withdrawal_cycle_audit_record(
    *, run_at, evaluated, paid, failed, total_amount, failures
):
    """Persists a WithdrawalCycleRun (+ one WithdrawalCycleFailure per
    real per-request exception) for one completed cycle -- called before
    the systemic-failure raise below, not after, so the one cycle most
    worth an audit record (every request failed) doesn't lose it along
    with the exception. Mirrors apps.commissions.tasks
    ._persist_cycle_audit_record's own best-effort fallback: a failed
    write here is logged, not allowed to mask the real cycle outcome."""
    try:
        cycle_run = WithdrawalCycleRun.objects.create(
            run_at=run_at,
            evaluated=evaluated,
            paid=paid,
            failed=failed,
            total_amount=total_amount,
        )
        if failures:
            WithdrawalCycleFailure.objects.bulk_create(
                WithdrawalCycleFailure(
                    cycle_run=cycle_run,
                    withdrawal_request_id=withdrawal_request_id,
                    distributor_id=distributor_id,
                    net_amount=net_amount,
                    error=error,
                )
                for withdrawal_request_id, distributor_id, net_amount, error in failures
            )
    except Exception:
        logger.exception(
            "%s: failed to persist the audit-trail record for run_at=%s -- "
            "continuing regardless, since a missing audit row is a lesser "
            "problem than losing the raise/return that follows.",
            WITHDRAWAL_PAYOUT_TASK_NAME,
            run_at,
        )


@shared_task
def process_withdrawal_payouts():
    """Task 16f PR 2. Batch driver for the Friday payout cycle: runs
    process_withdrawal_payout for every WithdrawalRequest that's
    approved_debited or queued_for_payout. Shape-mirrors apps.commissions
    .tasks._run_commission_cycle (fixed run_at, per-iteration-renewed
    lock, per-row exception isolation, systemic-failure guard, audit
    record persisted before that guard's raise) but is deliberately a
    separate, hand-written driver, not a call into that shared function
    -- _run_commission_cycle's process_one contract is
    `process_one(Distributor(pk=id), run_at) -> Decimal` (amount earned);
    this iterates WithdrawalRequest rows and needs "did this transfer
    resolve", not an amount, and its audit model is WithdrawalCycleRun/
    Failure, not CommissionCycleRun/Failure (see those models' own
    docstrings for why they're not merged into one job_name-discriminated
    pair). This was 16f's original doubt-driven-development decision, not
    an oversight.

    process_withdrawal_payout only ever reads .pk off the object passed
    to it before internally locking and re-fetching the real row (see its
    own and claim_for_payout's/apply_verified_transfer_outcome's
    docstrings) -- so, like _run_commission_cycle's own
    Distributor(pk=id) stub convention, passing an unsaved
    WithdrawalRequest(pk=id) stub here is safe by construction, not by
    convention alone; this was explicitly re-verified against the actual
    PR 1 source during this task's doubt-driven-development pass rather
    than assumed to hold just because the commission drivers do the same
    thing.

    paid/total_amount are NOT accumulated from process_withdrawal_
    payout's per-row return value during the loop (doubt-driven-
    development finding, 2026-07-24): that return value can't distinguish
    "this call just paid the row" from "the row was already paid before
    this call started" (e.g. resolved by a genuinely concurrent process),
    which would risk double-counting the same real payment across two
    cycle records. Instead, paid/total_amount are derived from one
    aggregate query over every row this cycle evaluated, after the loop
    finishes -- one extra query per cycle, not per row, so this doesn't
    reopen the flat-query-count goal Task 13's driver already established."""
    if not cache.add(
        WITHDRAWAL_PAYOUT_LOCK_KEY, "1", WITHDRAWAL_PAYOUT_LOCK_TIMEOUT_SECONDS
    ):
        logger.warning(
            "%s: previous cycle still in progress (lock held) -- skipping "
            "this trigger rather than running a second concurrent cycle.",
            WITHDRAWAL_PAYOUT_TASK_NAME,
        )
        return {"skipped": True, "reason": "previous cycle still in progress"}

    try:
        try:
            _sync_withdrawal_day_crontab()
        except Exception:
            logger.exception(
                "%s: failed to sync the PeriodicTask crontab -- continuing "
                "with this cycle's payouts regardless, since a stale "
                "schedule is a lesser problem than skipping payouts.",
                WITHDRAWAL_PAYOUT_TASK_NAME,
            )

        run_at = timezone.now()
        evaluated = 0
        failed = 0
        processed_ids = []
        failures = (
            []
        )  # [(withdrawal_request_id, distributor_id, net_amount, error), ...]

        for withdrawal_request_id in _withdrawal_payout_ids_query():
            evaluated += 1
            # Renews the lock's TTL on every iteration, not just at
            # acquisition -- mirrors _run_commission_cycle exactly, same
            # reasoning: a cycle still alive but running past the
            # timeout would otherwise lose the lock mid-cycle.
            cache.set(
                WITHDRAWAL_PAYOUT_LOCK_KEY,
                "1",
                WITHDRAWAL_PAYOUT_LOCK_TIMEOUT_SECONDS,
            )
            try:
                process_withdrawal_payout(WithdrawalRequest(pk=withdrawal_request_id))
                processed_ids.append(withdrawal_request_id)
            except Exception as exc:
                failed += 1
                distributor_id, net_amount = _snapshot_for_failure_record(
                    withdrawal_request_id
                )
                failures.append(
                    (withdrawal_request_id, distributor_id, net_amount, str(exc))
                )
                logger.exception(
                    "%s: withdrawal_request_id=%s failed this cycle (run_at=%s).",
                    WITHDRAWAL_PAYOUT_TASK_NAME,
                    withdrawal_request_id,
                    run_at,
                )
                continue

        aggregate = WithdrawalRequest.objects.filter(
            pk__in=processed_ids, status=WithdrawalRequest.Status.PAID
        ).aggregate(paid=Count("pk"), total_amount=Sum("net_amount"))
        paid = aggregate["paid"] or 0
        total_amount = aggregate["total_amount"] or Decimal("0.00")

        # Persisted before the systemic-failure raise below, not after --
        # a cycle where every request failed is exactly the scenario
        # most worth a durable record.
        _persist_withdrawal_cycle_audit_record(
            run_at=run_at,
            evaluated=evaluated,
            paid=paid,
            failed=failed,
            total_amount=total_amount,
            failures=failures,
        )

        if evaluated and failed == evaluated:
            raise RuntimeError(
                f"{WITHDRAWAL_PAYOUT_TASK_NAME}: every one of {evaluated} "
                f"evaluated withdrawal requests failed this cycle (run_at="
                f"{run_at.isoformat()}) -- likely a systemic bug, not "
                "routine per-request skips. See preceding per-request log "
                "entries for the individual exceptions."
            )

        logger.info(
            "%s: run_at=%s evaluated=%s paid=%s failed=%s total_amount=%s",
            WITHDRAWAL_PAYOUT_TASK_NAME,
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
        cache.delete(WITHDRAWAL_PAYOUT_LOCK_KEY)


# Task 16f PR 3. Reference prefix from apps.withdrawal.services
# ._generate_transfer_reference -- stable, matches what claim_for_payout
# (PR 1) pins onto WithdrawalRequest.paystack_transfer_reference. Confirmed
# against Paystack's own docs (docs-v2.paystack.com, 2026-07-24) that
# transfer webhook events use the exact strings below and the same
# x-paystack-signature HMAC-SHA512 scheme already implemented for
# charge.success -- see apps/distributors/views.py::paystack_webhook,
# which dispatches these to process_transfer_webhook_task below.
#
# code-review-and-quality note (2026-07-24): the event names and
# signature scheme are confirmed against real Paystack sources above, but
# the exact payload path (payload["data"]["reference"], mirroring
# charge.success's own webhook shape) has NOT been observed from a real
# live transfer webhook delivery -- Paystack Transfers are still blocked
# on this sandbox account's tier (project_paystack_transfer_account_tier_
# blocked memory), so this is inferred from the official Transfer
# resource schema (which has a top-level `reference` field) plus this
# project's own established webhook convention, not directly verified.
# If wrong, every transfer webhook falls through to the "unrecognized/
# missing transfer reference" warning log in paystack_webhook -- which
# exists specifically so this exact class of mistaken assumption surfaces
# loudly on the very first real delivery, not silently.
TRANSFER_WEBHOOK_EVENTS = {"transfer.success", "transfer.failed", "transfer.reversed"}


@shared_task
def process_transfer_webhook_task(reference: str) -> None:
    """Task 16f PR 3. Mirrors apps.distributors.tasks
    .consume_didit_result_task's shape: the webhook view only extracts
    the reference and enqueues this before responding, since
    verify_transfer's own REQUEST_TIMEOUT_SECONDS alone risks exceeding a
    webhook provider's response-time budget if called inline.

    Never trusts the webhook payload's own claimed status (doubt-driven-
    development contract, 2026-07-24) -- re-fetches the authoritative
    outcome via verify_transfer, matching apply_verified_transfer_outcome's
    own documented contract (it's also called this way by the batch
    driver's resume path, so this task is not a second, differently-
    trusted path into the same transition).

    Deliberately never raises: Paystack retries a webhook delivery for up
    to 72 hours on any non-2xx response (apps.distributors.views
    .paystack_webhook's own docstring), and every failure mode here (row
    not found, Paystack error, row deleted mid-call) is one this system
    will never resolve by retrying the same delivery -- the webhook view
    has already returned 200 by the time this runs, so there is nothing
    left to communicate back to Paystack. Every such case still logs, and
    the Friday batch driver's own resume-via-poll path remains the
    correctness backstop regardless -- this task is a latency
    optimization, not the sole path to resolution."""
    try:
        withdrawal_request = WithdrawalRequest.objects.get(
            paystack_transfer_reference=reference
        )
    except WithdrawalRequest.DoesNotExist:
        logger.warning(
            "process_transfer_webhook_task: no WithdrawalRequest found for "
            "reference=%s",
            reference,
        )
        return

    try:
        # CodeRabbit finding, PR #21: verify_transfer's own
        # response.json()["data"] can raise a bare KeyError/TypeError if
        # Paystack ever returns a 200 with an unexpected JSON shape --
        # that's not a PaystackError (no HTTP failure occurred), so it
        # wasn't covered by the except below until this fix. Caught here
        # alongside PaystackError since, from this task's perspective,
        # both mean the same thing: no usable outcome, defer to Friday.
        result = verify_transfer(reference)
    except (PaystackError, KeyError, TypeError):
        logger.exception(
            "process_transfer_webhook_task: verify_transfer failed for "
            "reference=%s -- leaving queued_for_payout for the next "
            "Friday batch cycle to resolve via its own resume path",
            reference,
        )
        return

    # .get(), not ["status"] (CodeRabbit finding, PR #21): a syntactically
    # valid but incomplete response (missing "status") must not raise --
    # None safely falls through apply_verified_transfer_outcome's own
    # existing "any other status is a no-op" branch, the same safe-by-
    # default handling already established for a genuinely unrecognized
    # status string.
    try:
        apply_verified_transfer_outcome(withdrawal_request, result.get("status"))
    except WithdrawalRequestNotFound:
        # The row existed at the lookup above but was deleted during the
        # verify_transfer call (e.g. a Distributor cascade-delete) --
        # nothing left to update.
        logger.warning(
            "process_transfer_webhook_task: WithdrawalRequest for "
            "reference=%s no longer exists by the time verify_transfer "
            "returned",
            reference,
        )
