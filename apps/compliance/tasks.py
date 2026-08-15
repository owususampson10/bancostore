import logging
from datetime import timedelta

from django.utils import timezone

from celery import shared_task
from constance import config

from apps.platform_settings.models import PlatformSettingChange

from .services import _HISTORY_TRACKED_MODELS

logger = logging.getLogger(__name__)


@shared_task
def cleanup_expired_audit_records():
    """Task 47e, source doc 13.12 "Audit Log Retention Period (days)".
    Deletes audit-log rows older than the live, admin-editable
    AUDIT_LOG_RETENTION_PERIOD_DAYS window -- every HistoricalRecords()
    shadow table this codebase has (Category/Product/Distributor/Order/
    WithdrawalRequest) plus PlatformSettingChange (Task 47d's own bespoke
    audit log for constance settings, conceptually the same audit-log
    initiative even though it isn't HistoricalRecords()-based).

    No lock, unlike apps.orders.tasks.auto_cancel_unpaid_orders or
    apps.reporting.tasks.compute_yesterdays_rollup -- mirrors
    apps.distributors.tasks.cleanup_expired_pending_registrations
    instead, this codebase's own closest precedent for a straight bulk
    "delete rows older than a cutoff" job. A plain .filter().delete() is
    naturally idempotent: two overlapping runs each recompute the same
    cutoff and race to delete the same (or an already-gone) row set --
    no double-delete, no corruption, nothing a lock would actually
    prevent, unlike the per-entity state-machine jobs above where an
    in-flight write really could collide with a concurrent one.

    No per-table audit trail of the cleanup itself (unlike OrderCycleRun/
    CommissionCycleRun) -- there's no per-row failure mode to isolate
    here (a bulk DELETE either succeeds or the whole task errors, Celery's
    own retry/alerting already covers that), so a CycleRun-style model
    would just be complexity with nothing real to report beyond the one
    summary log line below."""
    cutoff = timezone.now() - timedelta(days=config.AUDIT_LOG_RETENTION_PERIOD_DAYS)

    # Reuses services._HISTORY_TRACKED_MODELS (the same 5 models the
    # unified audit-log screen reads from) instead of a second hardcoded
    # list -- a 6th HistoricalRecords() model only needs to be added once.
    deleted_counts = {
        label: model.history.filter(history_date__lt=cutoff).delete()[0]
        for label, model in _HISTORY_TRACKED_MODELS.items()
    }
    deleted_counts["PlatformSettingChange"] = PlatformSettingChange.objects.filter(
        changed_at__lt=cutoff
    ).delete()[0]
    # The on-call question this answers: "is the retention job actually
    # running, and is it deleting anything?" -- without this, that's a
    # database query across 6 tables, not a log search.
    logger.info(
        "cleanup_expired_audit_records: cutoff=%s deleted=%s",
        cutoff.isoformat(),
        deleted_counts,
    )
    return deleted_counts
