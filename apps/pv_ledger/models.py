from django.db import models

from apps.binary_tree.models import BinaryTreeEdge


class PvLedger(models.Model):
    """Event-driven per-leg PV aggregate for one distributor (see SPEC.md
    Scale Architecture). Purchases increment these counters directly at
    write time -- the 10-minute binary bonus task only ever reads them.
    """

    distributor = models.OneToOneField(
        "distributors.Distributor",
        on_delete=models.CASCADE,
        related_name="pv_ledger",
    )
    left_leg_pv = models.PositiveIntegerField(default=0)
    right_leg_pv = models.PositiveIntegerField(default=0)

    def __str__(self):
        return (
            f"PvLedger<{self.distributor_id} "
            f"L={self.left_leg_pv} R={self.right_leg_pv}>"
        )


class MonthlyPersonalPv(models.Model):
    """How much PV `distributor` has personally generated in one calendar
    month -- distinct from PvLedger, which tracks PV credited to a
    distributor's LEGS from their downline's purchases. Needed for the
    Binary/Matching Bonus "minimum personal PV this month" eligibility
    rule (MIN_MONTHLY_PERSONAL_PV). `period` is always the first day of
    the month, both as the natural key for get_or_create and so a simple
    equality/range filter is enough to query a specific month."""

    distributor = models.ForeignKey(
        "distributors.Distributor",
        on_delete=models.CASCADE,
        related_name="monthly_personal_pv",
    )
    period = models.DateField()
    pv = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["distributor", "period"],
                name="unique_distributor_period_personal_pv",
            )
        ]

    def __str__(self):
        return f"MonthlyPersonalPv<{self.distributor_id} {self.period} pv={self.pv}>"


class PvDailyBucket(models.Model):
    """One dated, per-leg PV bucket for one ancestor -- the unit the
    Binary Bonus carry-forward/expiry mechanism (Task 13c/13d) consumes
    from FIFO and expires after PV_CARRY_FORWARD_EXPIRY_DAYS, instead of
    PvLedger's undated running total which can't express "this PV is
    180 days old, expire it" on its own.

    `leg` reuses BinaryTreeEdge.Leg rather than redefining an equivalent
    enum, since a bucket's leg has the same meaning (which of the
    ancestor's two legs this PV falls under) and must never drift out of
    sync with it.

    Rows are created lazily on first credit for a given
    (distributor, leg, date) -- unlike PvLedger, which always pre-exists
    once a distributor is placed, a bucket for "today" doesn't exist
    until the first purchase credits it. See
    apps.pv_ledger.services._credit_daily_buckets for the bulk-safe
    create-or-increment mechanism this requires."""

    distributor = models.ForeignKey(
        "distributors.Distributor",
        on_delete=models.CASCADE,
        related_name="pv_daily_buckets",
    )
    leg = models.CharField(max_length=1, choices=BinaryTreeEdge.Leg.choices)
    date = models.DateField()
    pv = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["distributor", "leg", "date"],
                name="unique_distributor_leg_date_pv_bucket",
            ),
            # PositiveIntegerField alone isn't a real DB-level guarantee
            # against this specific class of bug: bulk F()-relative
            # UPDATEs (as apps.pv_ledger.services.consume_leg_pv_fifo
            # uses) bypass Django's model-level field validation
            # entirely, and MySQL's UNSIGNED enforcement has no SQLite
            # equivalent -- meaning the whole test suite, which runs
            # against SQLite, would never catch a consumption bug that
            # drove pv negative. This constraint is the actual guardrail.
            models.CheckConstraint(
                check=models.Q(pv__gte=0), name="pv_daily_bucket_pv_gte_0"
            ),
        ]
        indexes = [
            # The carry-forward cycle's expiry step queries "this
            # distributor's buckets older than N days" -- covered by the
            # unique constraint's own index for single-distributor
            # lookups, but an explicit (distributor, date) index also
            # serves a plain date-range scan without the leg column
            # forcing extra index entries to be read.
            models.Index(fields=["distributor", "date"]),
        ]

    def __str__(self):
        return (
            f"PvDailyBucket<{self.distributor_id} {self.leg} {self.date} "
            f"pv={self.pv}>"
        )
