from django.db import models


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
