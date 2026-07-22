from django.db import models


class CommissionCycleRun(models.Model):
    """One durable row per commission batch-driver cycle that actually ran
    (not a skipped/overlap no-op -- that path returns before run_at is
    even generated). Originally `BinaryBonusCycleRun`, Binary-Bonus-only
    (Task 13, 2026-07-22) after a dedicated security-and-hardening
    review: these are financial jobs with no human review per cycle, and
    the only prior record of what a given run did was ephemeral Python
    logging (no LOGGING config exists in this project) plus Celery's
    Redis result backend (1-day TTL, not queryable). Generalized across
    bonus types the same day, once Task 14's Matching Bonus needed the
    identical guarantee -- a `job_name` discriminator instead of a second
    copy-pasted model pair, since Direct Referral Bonus is a plausible
    third consumer later and this pattern shouldn't be re-invented per
    bonus type. `job_name` is each task's own TASK_NAME constant (see
    apps/commissions/tasks.py), not a separate naming scheme.

    Uniqueness is scoped to (job_name, run_at), not run_at alone --
    two different jobs' independently-generated timezone.now() values
    are not guaranteed distinct just because collision is improbable."""

    job_name = models.CharField(max_length=50)
    run_at = models.DateTimeField()
    evaluated = models.PositiveIntegerField()
    paid = models.PositiveIntegerField()
    failed = models.PositiveIntegerField()
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-run_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["job_name", "run_at"], name="unique_job_name_run_at"
            )
        ]

    def __str__(self):
        return f"{self.job_name} cycle {self.run_at.isoformat()}"


class CommissionCycleFailure(models.Model):
    """One row per distributor whose per-distributor processing call
    raised during a cycle -- NOT one row per routine zero-payout skip
    (ineligible / zero-weak-leg / cap-exhausted / no-downline-earnings
    are expected, high-volume outcomes already covered by DEBUG logging
    in apps/commissions/services.py; persisting every one of those here
    would defeat the point of that existing design and bloat this table
    at this platform's stated scale).

    Deliberately not a ForeignKey to Distributor -- one of the failure
    modes this table exists to record is a distributor row deleted
    mid-cycle (see tests/unit/commissions/test_binary_bonus_task.py's
    "ghost" test), and an audit record of that must survive the deletion
    rather than being unrepresentable or cascading away with it."""

    cycle_run = models.ForeignKey(
        CommissionCycleRun, on_delete=models.CASCADE, related_name="failures"
    )
    distributor_id = models.PositiveIntegerField()
    error = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["distributor_id"]

    def __str__(self):
        return f"distributor_id={self.distributor_id}"
