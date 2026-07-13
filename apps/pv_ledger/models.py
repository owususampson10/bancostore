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
