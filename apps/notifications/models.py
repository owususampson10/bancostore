from django.db import models

from simple_history.models import HistoricalRecords


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

    # Task 21d-iv: one canonical icon/color per event type, not a
    # per-device choice -- reconciling the fetched Stitch mockups, whose
    # desktop and mobile screens disagreed with each other on the
    # withdrawal-approved icon (check_circle/green vs.
    # account_balance_wallet/gray). Kept in Python (not duplicated as a
    # long Django-template if/elif chain) so it's one source of truth and
    # a test can assert every EventType has an entry.
    _ICON_BY_EVENT_TYPE = {
        EventType.DOWNLINE_JOINED: ("person", "bg-primary-container/10 text-primary"),
        EventType.BINARY_BONUS_CREDITED: ("payments", "bg-primary/10 text-primary"),
        EventType.REFERRAL_BONUS_PAID: (
            "redeem",
            "bg-secondary-container text-secondary",
        ),
        EventType.WITHDRAWAL_APPROVED: (
            "check_circle",
            "bg-success/10 text-success",
        ),
        EventType.KYC_DECIDED: (
            "verified_user",
            "bg-secondary-container text-secondary",
        ),
        EventType.PV_EXPIRING: ("warning", "bg-error-container/30 text-error"),
    }

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

    @property
    def icon_name(self) -> str:
        return self._ICON_BY_EVENT_TYPE[self.event_type][0]

    @property
    def icon_classes(self) -> str:
        return self._ICON_BY_EVENT_TYPE[self.event_type][1]


class AdminNotification(models.Model):
    """Task 61c. An admin-portal-facing notification, for the bell in the
    admin shell.

    SEPARATE from Notification above rather than a widened version of it.
    That model is FK'd to Distributor and documented as having "exactly 6
    event types because Section 6.6 names exactly 6" -- a real, narrow
    contract. Making its distributor nullable and bolting on an audience
    flag would blur that into "notifications, for someone" and leave every
    reader checking which kind they have.

    ONE SHARED STREAM, not one per staff account. "The admin" here is a
    role, not a person: a new order is a fact about the shop, and every
    admin should see the same list rather than each getting a private copy
    of the same event. The accepted cost is that `is_read` is shared too --
    when one admin reads a notification it is read for all of them. For a
    shop with a handful of staff that is the behaviour you want; if this
    ever grows per-user read state, that is a real schema change and a
    real decision, not something to slide in later.
    """

    class EventType(models.TextChoices):
        NEW_ORDER = "new_order", "New order paid"

    _ICON_BY_EVENT_TYPE = {
        EventType.NEW_ORDER: ("shopping_bag", "bg-primary/10 text-primary"),
    }

    event_type = models.CharField(max_length=32, choices=EventType.choices)
    message = models.CharField(max_length=255)
    # SET_NULL, not CASCADE: an admin reading their bell history should
    # still see that an order arrived even if the row was later removed.
    # Losing the audit line entirely would be worse than losing the link.
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_notifications",
    )
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # "-pk" as the tie-breaker, not "-created_at" alone: Task 15d's
        # pagination bug was exactly this -- two rows sharing a timestamp
        # could be skipped or repeated across a page boundary.
        ordering = ["-created_at", "-pk"]
        indexes = [models.Index(fields=["is_read", "-created_at"])]

    def __str__(self):
        return f"{self.get_event_type_display()}: {self.message}"

    @property
    def icon(self):
        """One canonical icon/colour per event type, mirroring
        Notification._ICON_BY_EVENT_TYPE. Falls back rather than raising
        so a new event type renders a plain bell instead of 500-ing the
        whole admin shell."""
        return self._ICON_BY_EVENT_TYPE.get(
            self.event_type, ("notifications", "bg-surface-container text-on-surface")
        )

    @classmethod
    def unread_count(cls) -> int:
        return cls.objects.filter(is_read=False).count()


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


class NotificationTemplate(models.Model):
    """Task 48a (source doc Section 13.8). Lets an admin edit the wording
    of an outbound notification without a code change/deploy. One row
    per specific outbound message -- e.g. withdrawal_approved and
    kyc_approved are separate rows even though both fire from a
    "decision" action, because their channels and content genuinely
    differ (see each Key member's own label for its channel).

    `key` is a closed enum, not a freeform field: a call site looks a
    template up by a fixed Key member, so editing wording is possible
    but inventing a new notification type that no code path reads is
    not. Pre-seeded via migration for every currently-wired send site
    this sub-task covers (0005_seed_notification_templates) -- this
    project's own established "pre-seed a fixed row set, don't
    get_or_create at first-use" convention (IrIdSequence, EscrowLedger).
    A send site that finds no row for its key falls back to a safe
    hardcoded default (see apps.notifications.rendering.render_or_default)
    rather than failing the underlying business operation -- a
    notification wording lookup must never be the reason a real payment/
    KYC/OTP flow breaks.

    Security note (Task 48a's own mandatory review, see tasks/todo.md):
    `body`/`subject` are admin-entered text rendered via
    apps.notifications.rendering.render_template, a bespoke regex
    substitution that can only ever emit values explicitly passed in by
    the CALLER's own context dict -- never attribute/method access,
    never code execution, regardless of what an admin writes in these
    fields. The two invariants that keep this safe going forward: (1)
    call sites must never put a secret/PII value into that context dict
    that shouldn't be admin-visible through some future new placeholder
    key, and (2) `body`/`subject` must never be rendered with Django's
    `|safe` filter anywhere (every current consumer -- SMS, plain-text
    email, the in-app notification bell -- is a context where Django's
    own auto-escaping or plain-text transport already makes injected
    HTML/script content inert). `subject` additionally rejects embedded
    `\\r`/`\\n` at save time (NotificationTemplateForm.clean()) -- Django's
    send_mail() already blocks multi-line headers at send time
    (BadHeaderError), but that exception would otherwise be silently
    swallowed by every call site's own best-effort try/except, quietly
    breaking that notification type at every future send rather than
    surfacing the mistake to the admin who made it."""

    class Key(models.TextChoices):
        OTP_CODE = "otp_code", "OTP Verification Code (SMS)"
        WITHDRAWAL_APPROVED = "withdrawal_approved", "Withdrawal Approved (SMS)"
        WITHDRAWAL_REJECTED = "withdrawal_rejected", "Withdrawal Rejected (SMS)"
        WITHDRAWAL_PAID = "withdrawal_paid", "Withdrawal Paid (SMS)"
        WITHDRAWAL_REVERSED = "withdrawal_reversed", "Withdrawal Reversed (SMS)"
        KYC_APPROVED = "kyc_approved", "KYC Approved (In-App Notification)"
        KYC_REJECTED = "kyc_rejected", "KYC Rejected (In-App Notification)"
        # Task 48c. Matching Bonus deliberately has no member here -- Task
        # 21d already excluded it from every notification channel (Section
        # 6.6 never names it), and this task doesn't invent a new send site
        # for it. _send_confirmation_notifications/_send_auto_cancel_
        # notification/_send_stock_unavailable_notification (Task 17/18's
        # own distinct checkout-flow messages) are also deliberately out of
        # scope -- 48c's acceptance criteria names "order status update",
        # which is specifically apps.orders.services._send_order_status_
        # notification (used by advance_order_status and
        # cancel_or_refund_order), not every order-related message.
        BINARY_BONUS_CREDITED = (
            "binary_bonus_credited",
            "Binary Bonus Credited (In-App Notification)",
        )
        DOWNLINE_JOINED = "downline_joined", "New Downline Member (In-App Notification)"
        DIRECT_REFERRAL_BONUS_CREDITED_SMS = (
            "direct_referral_bonus_credited_sms",
            "Direct Referral Bonus Credited (SMS)",
        )
        DIRECT_REFERRAL_BONUS_CREDITED_INAPP = (
            "direct_referral_bonus_credited_inapp",
            "Direct Referral Bonus Credited (In-App Notification)",
        )
        PV_EXPIRING = "pv_expiring", "PV Approaching Expiry (In-App Notification)"
        ORDER_STATUS_UPDATE_SMS = "order_status_update_sms", "Order Status Update (SMS)"
        ORDER_STATUS_UPDATE_EMAIL = (
            "order_status_update_email",
            "Order Status Update (Email)",
        )
        # Task 60. Task 48c migrated every other send site onto this
        # system but missed apps.orders.services::
        # _send_confirmation_notifications, which stayed a hardcoded
        # f-string -- so an admin could reword "your order is now
        # Dispatched" but not the confirmation message a customer sees on
        # every single purchase.
        # Task 61a/61b: admin-facing, unlike every key above it. The
        # recipient is a single configured address/number, not the
        # customer on the order.
        ADMIN_NEW_ORDER_EMAIL = (
            "admin_new_order_email",
            "New Order -- Admin Alert (Email)",
        )
        ADMIN_NEW_ORDER_SMS = (
            "admin_new_order_sms",
            "New Order -- Admin Alert (SMS)",
        )
        ORDER_CONFIRMED_SMS = "order_confirmed_sms", "Order Confirmed (SMS)"
        ORDER_CONFIRMED_EMAIL = (
            "order_confirmed_email",
            "Order Confirmed (Email receipt)",
        )

    key = models.CharField(max_length=40, choices=Key.choices, unique=True)
    # Email-only -- blank for the SMS/in-app keys above. Whether `subject`
    # applies depends on which channel `key` implies, which isn't its own
    # model field (see the class docstring); enforced as blank-for-non-
    # email in NotificationTemplateForm.clean(), matching DiscountCode/
    # Banner's own "form is the real cross-field enforcement" convention.
    subject = models.CharField(max_length=200, blank=True, default="")
    body = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)
    # A security-review finding (Task 48a): this governs real customer-
    # facing financial/KYC wording, the same class of admin-editable
    # content Task 47d/47e already built a real audit trail for
    # (Category/Product/PlatformSettingChange) -- an edit here belongs in
    # that same unified audit log, not left untracked.
    history = HistoricalRecords()

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.get_key_display()


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
