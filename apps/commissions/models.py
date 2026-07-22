from django.db import models


class BinaryBonusCycleRun(models.Model):
    """One durable row per apps/commissions/tasks.py::calculate_binary_bonus
    invocation that actually ran a cycle (not a skipped/overlap no-op --
    that path returns before run_at is even generated). Added 2026-07-22
    after a dedicated security-and-hardening review: this is a financial
    job with no human review per cycle, and its only prior record of what
    a given run did was ephemeral Python logging (no LOGGING config exists
    in this project) plus Celery's Redis result backend (1-day TTL, not
    queryable). This is the record a support inquiry or incident review
    can still find weeks later."""

    run_at = models.DateTimeField(unique=True)
    evaluated = models.PositiveIntegerField()
    paid = models.PositiveIntegerField()
    failed = models.PositiveIntegerField()
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-run_at"]

    def __str__(self):
        return f"Binary Bonus cycle {self.run_at.isoformat()}"


class BinaryBonusCycleFailure(models.Model):
    """One row per distributor whose process_binary_bonus_for_distributor
    call raised during a cycle -- NOT one row per routine zero-payout skip
    (ineligible / zero-weak-leg / cap-exhausted are expected, high-volume
    outcomes already covered by DEBUG logging in apps/commissions/
    services.py; persisting every one of those here would defeat the point
    of that existing design and bloat this table at this platform's stated
    scale).

    Deliberately not a ForeignKey to Distributor -- one of the failure
    modes this table exists to record is a distributor row deleted
    mid-cycle (see tests/unit/commissions/test_binary_bonus_task.py's
    "ghost" test), and an audit record of that must survive the deletion
    rather than being unrepresentable or cascading away with it."""

    cycle_run = models.ForeignKey(
        BinaryBonusCycleRun, on_delete=models.CASCADE, related_name="failures"
    )
    distributor_id = models.PositiveIntegerField()
    error = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["distributor_id"]

    def __str__(self):
        return f"distributor_id={self.distributor_id}"
