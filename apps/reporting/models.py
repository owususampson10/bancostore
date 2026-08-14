from django.db import models


class DailyOrderRollup(models.Model):
    """Task 46b (ADR-0010). One row per calendar day, computed by
    apps.reporting.services.compute_daily_rollup -- reports sum a bounded
    number of these rows for an admin-chosen date range instead of
    scanning the full Order table, so query cost is proportional to the
    number of DAYS requested, not the number of orders in this project's
    history.

    Revenue and status counts are computed the same way Task 27's Admin
    Dashboard already does (apps.admin_portal.views.dashboard): revenue
    excludes pending/cancelled/refunded orders (unpaid, or money already
    returned -- neither is real revenue), scoped to Order.created_at
    falling on this exact calendar day.

    Known, accepted staleness (ADR-0010's own Consequences section): if
    an order created on this date is later cancelled/refunded AFTER this
    row was computed, this row does not automatically update -- a
    backfill re-run (compute_daily_rollup is an idempotent upsert, not
    append-only) is needed to reflect it. Rolling up "yesterday" once
    nightly gives most same-day status changes (e.g. the 24-hour
    PENDING_ORDER_AUTO_CANCEL_HOURS window) time to settle before this
    ever runs, but does not eliminate the gap."""

    date = models.DateField(unique=True)

    revenue = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    orders_pending = models.PositiveIntegerField(default=0)
    orders_confirmed = models.PositiveIntegerField(default=0)
    orders_processing = models.PositiveIntegerField(default=0)
    orders_dispatched = models.PositiveIntegerField(default=0)
    orders_delivered = models.PositiveIntegerField(default=0)
    orders_cancelled = models.PositiveIntegerField(default=0)
    orders_refunded = models.PositiveIntegerField(default=0)

    # One column per Order.DeliveryZone choice, mirroring the
    # orders_<status> shape above -- both are small, fixed, stable enums
    # (7 statuses, 3 zones), so explicit typed columns stay queryable via
    # plain .aggregate(Sum(...)) across every DB backend this project
    # supports, unlike a JSONField which would need per-backend JSON path
    # querying for the same result.
    delivery_fees_kumasi = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    delivery_fees_accra = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )
    delivery_fees_other_regions = models.DecimalField(
        max_digits=12, decimal_places=2, default=0
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"DailyOrderRollup<{self.date}>"


class DailyProductSales(models.Model):
    """Task 46b (ADR-0010). One row per (date, product) -- the best-
    selling-products report sums these across an admin-chosen date range
    instead of scanning OrderItem directly, matching DailyOrderRollup's
    own reasoning above.

    PROTECT, matching OrderItem.product's own on_delete choice exactly:
    a product referenced by any order line (and therefore by any rollup
    row derived from one) can never be deleted, so this FK can never
    actually go stale through normal use -- product_name is still
    snapshotted here (mirroring OrderItem.product_name) to survive a
    later product RENAME, which PROTECT does nothing to prevent."""

    date = models.DateField()
    product = models.ForeignKey(
        "catalog.Product", on_delete=models.PROTECT, related_name="+"
    )
    product_name = models.CharField(max_length=255)
    units_sold = models.PositiveIntegerField(default=0)
    revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "-units_sold"]
        constraints = [
            models.UniqueConstraint(
                fields=["date", "product"],
                name="unique_daily_product_sales_date_product",
            ),
        ]

    def __str__(self):
        return f"DailyProductSales<{self.date} {self.product_name}>"


class ReportRollupRun(models.Model):
    """Task 46b (ADR-0010). One durable row per attempt to compute a
    single calendar day's DailyOrderRollup + DailyProductSales rows --
    both written inside one atomic transaction (all-or-nothing; see
    compute_daily_rollup's own docstring), so there's no partial-failure
    state to represent the way CommissionCycleRun/OrderCycleRun's
    per-entity Failure sub-tables exist for -- this job has no per-entity
    loop, just a single day's aggregate computation.

    rollup_date is the calendar day computed, distinct from run_at (when
    this attempt actually ran) -- a backfill re-running a past day's
    rollup has a run_at of "now" but a rollup_date from the past.
    Uniqueness is (rollup_date, run_at), not rollup_date alone: a failed
    nightly attempt followed by a successful manual backfill both belong
    in the durable history, not silently overwritten -- mirroring
    CommissionCycleRun's own (job_name, run_at) uniqueness reasoning."""

    rollup_date = models.DateField()
    run_at = models.DateTimeField()
    succeeded = models.BooleanField()
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-run_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["rollup_date", "run_at"], name="unique_rollup_date_run_at"
            )
        ]

    def __str__(self):
        return f"rollup for {self.rollup_date} at {self.run_at.isoformat()}"
