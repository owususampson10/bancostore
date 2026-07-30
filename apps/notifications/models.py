from django.db import models


class OTPCode(models.Model):
    class Purpose(models.TextChoices):
        REGISTRATION = "registration", "Registration"
        PASSWORD_RESET = "password_reset", "Password reset"

    phone_number = models.CharField(max_length=20, db_index=True)
    purpose = models.CharField(max_length=20, choices=Purpose.choices)
    code = models.CharField(max_length=6)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"OTPCode<{self.phone_number}, {self.purpose}>"


class Notification(models.Model):
    """A distributor-facing notification (Task 21d, Section 6.6 of the
    primary source doc) -- persisted so it survives to the next login,
    not only visible while a WebSocket happens to be connected. Created
    exclusively via apps.notifications.services.send_notification, never
    directly, so the create-then-push behavior stays in one place.

    Exactly 6 event types exist because Section 6.6 names exactly 6 --
    notably not the matching bonus (unlike the binary and referral
    bonuses), a deliberate exclusion, not an oversight."""

    class EventType(models.TextChoices):
        DOWNLINE_JOINED = "downline_joined", "New downline member"
        BINARY_BONUS_CREDITED = "binary_bonus_credited", "Binary bonus credited"
        REFERRAL_BONUS_PAID = "referral_bonus_paid", "Referral bonus paid"
        WITHDRAWAL_APPROVED = "withdrawal_approved", "Withdrawal approved"
        KYC_DECIDED = "kyc_decided", "KYC decision"
        PV_EXPIRING = "pv_expiring", "PV approaching expiry"

    distributor = models.ForeignKey(
        "distributors.Distributor",
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    # Pre-rendered at creation time with full context from the call site
    # (e.g. "Your KYC was approved" vs "...rejected: <reason>") rather
    # than reconstructed from event_type client-side -- generous
    # max_length so a real-world message is very unlikely to ever
    # overflow it.
    message = models.CharField(max_length=500)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            # The bell's notification list, newest first.
            models.Index(fields=["distributor", "-created_at"]),
            # The bell's unread-count badge -- a separate index since it
            # filters on is_read rather than ordering by created_at.
            models.Index(fields=["distributor", "is_read"]),
        ]

    def __str__(self):
        return (
            f"Notification<{self.distributor_id} {self.event_type} read={self.is_read}>"
        )


class NotificationCycleRun(models.Model):
    """Task 21d-iii. Mirrors apps.orders.models.OrderCycleRun's own
    audit-trail shape and rationale for
    apps.notifications.tasks.send_pv_expiry_notifications -- its own
    model, not a third consumer of CommissionCycleRun/OrderCycleRun:
    this job moves no money and isn't order-shaped either, so neither
    existing model's domain-specific fields (total_amount / cancelled)
    fit. `notified` is this job's own domain-appropriate name for what
    those call `paid`/`cancelled`.

    No job_name discriminator -- exactly one job writes here, matching
    OrderCycleRun/WithdrawalCycleRun's own reasoning for the same
    omission. Uniqueness is on run_at alone for the same reason."""

    run_at = models.DateTimeField(unique=True)
    evaluated = models.PositiveIntegerField()
    notified = models.PositiveIntegerField()
    failed = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-run_at"]

    def __str__(self):
        return f"pv-expiry cycle {self.run_at.isoformat()}"


class NotificationCycleFailure(models.Model):
    """One row per distributor whose per-distributor PV-expiry check
    raised during a cycle -- not one row per routine "not nearing
    expiry" or "already notified" skip (see OrderCycleFailure's own
    docstring for the identical reasoning)."""

    cycle_run = models.ForeignKey(
        NotificationCycleRun, on_delete=models.CASCADE, related_name="failures"
    )
    distributor_id = models.PositiveIntegerField()
    error = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["distributor_id"]

    def __str__(self):
        return f"distributor_id={self.distributor_id}"
