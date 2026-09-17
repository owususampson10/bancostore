from django.conf import settings
from django.db import models

from phonenumber_field.modelfields import PhoneNumberField
from simple_history.models import HistoricalRecords

from apps.catalog.models import Product


class Order(models.Model):
    """Task 17a. See docs/decisions/0005-checkout-cart-design.md for the
    full checkout design -- this model is schema only, no service logic
    (that's 17c/17d). Created in PENDING status at checkout confirmation,
    before payment succeeds (ADR-0005 decision 3) -- a real, visible row
    from that moment, not a ghost table like PendingRegistration."""

    class DeliveryMethod(models.TextChoices):
        HOME_DELIVERY = "home_delivery", "Home Delivery"
        PICKUP = "pickup", "Pickup"

    class DeliveryZone(models.TextChoices):
        KUMASI = "kumasi", "Kumasi"
        ACCRA = "accra", "Accra"
        OTHER_REGIONS = "other_regions", "Other Regions"

    # Full choice set per the source doc's Section 5.2 table, even though
    # Task 17 only ever writes PENDING/CONFIRMED -- Task 18 owns the rest
    # and shouldn't need a migration just to add choices.
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONFIRMED = "confirmed", "Confirmed"
        PROCESSING = "processing", "Processing"
        DISPATCHED = "dispatched", "Dispatched"
        DELIVERED = "delivered", "Delivered"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    # Guest checkout: customer is null (SPEC.md -- "checkout as guest or
    # with an account"). SET_NULL, not CASCADE/PROTECT: deleting a user
    # account must not destroy order/accounting history. Contact fields
    # below are always snapshotted on this row regardless, so a later
    # SET_NULL (no account-deletion feature exists yet to trigger this)
    # doesn't lose any support/audit-relevant information -- the only
    # thing it can't distinguish is "always a guest" vs. "account since
    # deleted," which nothing in this codebase currently needs to know.
    customer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders",
    )

    # Always snapshotted here, never looked up live off the account
    # afterward -- a customer/distributor updating their profile after
    # ordering must not change what a already-placed order shows. email
    # is optional: apps/distributors/forms.py's email field is already
    # `required=False` (SPEC.md Section 4: "for notifications only"), so a
    # logged-in distributor with no real email must still be able to check
    # out. Order confirmation email sending (17d) must skip a blank email
    # rather than invent a fake address, unlike the existing
    # Paystack-recipient-email workaround elsewhere in this codebase.
    full_name = models.CharField(max_length=255)
    phone_number = PhoneNumberField()
    email = models.EmailField(blank=True, default="")

    delivery_method = models.CharField(max_length=20, choices=DeliveryMethod.choices)
    # Blank for pickup, required for home delivery (enforced below) --
    # a direct customer selection at checkout, not derived from free-text
    # address/region matching (ADR-0005: avoids inventing a city-name-to-
    # zone mapping table for a flat 3-tier MVP scope).
    delivery_zone = models.CharField(
        max_length=20, choices=DeliveryZone.choices, blank=True, default=""
    )
    # Ghana's landmark-based addressing convention, matching
    # PendingRegistration's existing address/area/landmark shape rather
    # than inventing a new one. landmark stays optional even for home
    # delivery, matching PendingRegistration's own field.
    address = models.CharField(max_length=255, blank=True, default="")
    area = models.CharField(max_length=255, blank=True, default="")
    landmark = models.CharField(max_length=255, blank=True, default="")

    # All three snapshotted at order-creation time (17c) -- a later change
    # to a live Product.price or the DELIVERY_FEE_*/FREE_DELIVERY_THRESHOLD
    # constance settings must never retroactively change an
    # already-created order's stored figures. subtotal == sum(OrderItem
    # unit_price * quantity) cannot be enforced at the DB level (a
    # CheckConstraint can't reference another table) -- 17c/17d's own
    # tests are the actual guardrail for that identity, not this schema.
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    delivery_fee = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)

    # How much PV this order actually credited (0 if not a distributor
    # purchase, or not yet confirmed) -- set inside the same locked block
    # as apps.pv_ledger.services.record_purchase_pv at confirmation (17d),
    # never re-derived later from live Product.pv_value. This is the
    # durable answer to "did this order pay PV, and how much" without
    # needing to reconstruct it from apps.pv_ledger after the fact.
    pv_earned = models.PositiveIntegerField(default=0)

    # Task 44b. Snapshotted once at create_pending_order() time from the
    # live BACKORDERS_ENABLED + OUT_OF_STOCK_BEHAVIOUR="backorder"
    # constance settings -- confirm_order_payment reads THIS, never a
    # live re-check of those settings, so an admin disabling backorders
    # between Paystack capturing payment and a delayed webhook/callback
    # confirming the order can never wrongly cancel an order the
    # customer already paid for while backorders were on. A
    # doubt-driven-development review (2026-08-13) flagged this
    # asymmetry directly: unlike the live stock-quantity check (which
    # exists to protect against overselling and is safe to re-read live),
    # a live re-read of this flag has no protective purpose and can only
    # ever produce customer harm after payment capture.
    backorders_allowed_at_checkout = models.BooleanField(default=False)

    # Task 43b. Snapshotted once at create_pending_order() time -- never
    # re-derived from a live DiscountCode afterward, same "snapshot
    # everything" convention as subtotal/delivery_fee/total above. SET_NULL,
    # not CASCADE/PROTECT: deleting a DiscountCode must not destroy order/
    # accounting history, mirroring Banner's product/category FK precedent
    # (apps/promotions/models.py). discount_amount stays on the row even
    # after discount_code is nulled out, so past figures are never lost.
    discount_code = models.ForeignKey(
        "promotions.DiscountCode",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="orders",
    )
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    payment_reference = models.CharField(max_length=100, unique=True)
    # Task 18a: the real query shape the code-review-and-quality note
    # below was waiting for now exists -- Task 18d's auto-cancel task
    # filters pending orders older than a configured window
    # (status=PENDING, created_at__lt=cutoff), matching
    # WithdrawalRequest's own pre-emptive status index (Task 16b) for
    # this project's "hundreds of thousands of users" scale reasoning.
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # Task 62 follow-up (CodeRabbit, PR #93): when the receipt email was
    # actually sent. NULL on a confirmed order with an email address means
    # the receipt has not gone out yet, and resend_missing_order_receipts
    # will queue it again. Written only through a queryset .update(), so a
    # later plain order.save() of an instance loaded BEFORE the send would
    # reset it to NULL and send a second receipt -- every Order write in
    # this codebase passes update_fields today; keep it that way.
    receipt_email_sent_at = models.DateTimeField(null=True, blank=True)
    # How many times the sweep has re-queued this receipt. Capped, so an
    # order whose email can never be delivered stops being retried rather
    # than hogging every future sweep.
    receipt_email_sweeps = models.PositiveSmallIntegerField(default=0)
    # Task 46b (ADR-0010): indexed for the first time here -- both
    # order_management_queue's existing admin date-range filter
    # (_filtered_orders) and this task's new reporting rollup job scan
    # this column, and it had no index at all before this, a real,
    # pre-existing gap this project's own "hundreds of thousands of
    # users" scale reasoning (see the status field's own comment above)
    # already applies to.
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Task 18a (ADR-0006, Section 5.3: "Admin can update the order status
    # and add a tracking note"). Optional -- not every status update
    # needs one.
    tracking_note = models.TextField(blank=True, default="")

    # Money- and PII-adjacent (guest checkout stores raw contact info with
    # no account) -- SPEC.md's "log admin actions affecting money ... via
    # django-simple-history" applies directly, matching
    # WithdrawalRequest/Distributor's own precedent.
    history = HistoricalRecords()

    class Meta:
        indexes = [
            # The receipt sweep's query: unsent receipts in a recent
            # confirmed_at window. Almost every row has a sent time, so the
            # NULL prefix of this index stays tiny at any table size.
            models.Index(
                fields=["receipt_email_sent_at", "confirmed_at"],
                name="order_receipt_sweep_idx",
            ),
        ]
        constraints = [
            # Task 43b: total now accounts for discount_amount. Deliberately
            # NOT a "discount_code IS NULL implies discount_amount = 0"
            # constraint -- discount_code is SET_NULL on delete specifically
            # so a deleted DiscountCode doesn't erase the historical
            # discount_amount already charged on past orders (a
            # doubt-driven-development finding before this migration was
            # written: that constraint would reject the exact state this
            # FK's on_delete choice is designed to produce). discount_amount
            # <= subtotal is redundant with total__gte=0 given the other
            # three clauses, but kept as explicit defense-in-depth, matching
            # this codebase's stated CheckConstraint philosophy.
            models.CheckConstraint(
                check=(
                    models.Q(subtotal__gte=0)
                    & models.Q(delivery_fee__gte=0)
                    & models.Q(total__gte=0)
                    & models.Q(discount_amount__gte=0)
                    & models.Q(discount_amount__lte=models.F("subtotal"))
                    & models.Q(
                        total=models.F("subtotal")
                        + models.F("delivery_fee")
                        - models.F("discount_amount")
                    )
                ),
                name="order_amounts_sane",
            ),
            # Explicit parens around each OR-branch, matching
            # apps/withdrawal/models.py and apps/distributors/models.py's
            # existing both-or-neither constraints -- relying on bare `&`/
            # `|` precedence here would be a readability regression from
            # that established convention, even though it evaluates the
            # same either way.
            # Raw string literals, not DeliveryMethod.PICKUP/.HOME_DELIVERY --
            # a Meta.constraints list is evaluated in the Meta class body,
            # which cannot see names from the enclosing Order class body
            # (Order isn't even fully defined yet at this point). Every
            # other CheckConstraint precedent in this codebase
            # (WithdrawalRequest, Distributor) uses raw values for the same
            # reason.
            # CodeRabbit (PR #24): the pickup branch required
            # delivery_zone/address/area blank but never delivery_fee=0,
            # so a pickup order could persist with a positive fee --
            # violating "pickup is always free" (ADR-0005 decision 1).
            # Fixed by adding delivery_fee=0 to the pickup branch.
            models.CheckConstraint(
                check=(
                    models.Q(
                        delivery_method="pickup",
                        delivery_zone="",
                        address="",
                        area="",
                        delivery_fee=0,
                    )
                    | (
                        models.Q(delivery_method="home_delivery")
                        & ~models.Q(delivery_zone="")
                        & ~models.Q(address="")
                        & ~models.Q(area="")
                    )
                ),
                name="order_delivery_address_required_iff_home_delivery",
            ),
        ]

    def __str__(self):
        return f"Order<{self.pk} {self.status}>"


class OrderItem(models.Model):
    """Task 17a. product_name/unit_price/unit_pv are all snapshots -- a
    later Product rename/price/pv_value change must never alter an
    already-placed order's history. No variant tracking: variant display
    on the product page (apps/catalog/models.py::ProductVariant) is
    purely informational (plain, non-interactive spans in
    templates/catalog/product_detail.html) -- Task 7 explicitly scoped
    stock tracking to the Product level only and deferred real variant
    tracking as separate future scope, so there is nothing yet for an
    order line to record a variant selection *of*. Revisit together with
    interactive variant selection if that's ever built -- not silently
    dropped, just genuinely out of this task's scope."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    # PROTECT, not CASCADE: a Product must not be deletable while order
    # history still references it.
    product = models.ForeignKey(
        Product, on_delete=models.PROTECT, related_name="order_items"
    )
    product_name = models.CharField(max_length=255)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    unit_pv = models.PositiveIntegerField(default=0)

    # Task 44b. 0 until confirm_order_payment runs; from then on, the
    # REAL amount apps.catalog.services.decrement_stock actually removed
    # from Product.stock for this line -- equal to `quantity` unless the
    # order's own backorders_allowed_at_checkout let a backordered line
    # clamp instead of raising InsufficientStockError, in which case this
    # is whatever stock was actually on hand (possibly less than
    # `quantity`, possibly 0). cancel_or_refund_order's restock logic
    # (Task 18b) reverses THIS, never `quantity` -- a doubt-driven-
    # development review (2026-08-13) found that restocking the full
    # ordered quantity for a backordered line would silently credit
    # Product.stock with units that were never actually removed from it.
    stock_decremented = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(quantity__gte=1) & models.Q(unit_price__gte=0),
                name="order_item_quantity_and_price_sane",
            ),
            # stock_decremented can never exceed what was actually
            # ordered -- the DB-level guarantee backing the docstring
            # above, matching this codebase's established
            # defense-in-depth CheckConstraint philosophy (Order's own
            # Meta.constraints, PvDailyBucket, Wallet).
            models.CheckConstraint(
                check=models.Q(stock_decremented__lte=models.F("quantity")),
                name="order_item_stock_decremented_not_over_quantity",
            ),
        ]

    @property
    def line_total(self):
        # Mirrors apps/orders/cart.py::CartLine.line_total's exact
        # computation -- unit_price is already the snapshotted price, so
        # this never re-reads a live Product.price.
        return self.unit_price * self.quantity

    def __str__(self):
        return (
            f"OrderItem<order={self.order_id} "
            f"product={self.product_name} x{self.quantity}>"
        )


class OrderCycleRun(models.Model):
    """Task 18d. Mirrors CommissionCycleRun/WithdrawalCycleRun's own
    audit-trail shape and rationale (financial/scheduled jobs with no
    human review per cycle need a durable record, not just ephemeral
    logging) for the auto-cancel-unpaid-orders batch driver
    (apps.orders.tasks.auto_cancel_unpaid_orders) -- but as its own
    model, not a third consumer of either: this job moves no money at
    all, so the `total_amount` field both of those require doesn't apply
    here, and CommissionCycleFailure.distributor_id /
    WithdrawalCycleFailure.withdrawal_request_id can't hold an order_id
    without corrupting either field's meaning. `cancelled` is this job's
    own domain-appropriate name for what CommissionCycleRun calls `paid`.

    No job_name discriminator -- exactly one job writes here, matching
    WithdrawalCycleRun's own reasoning for the same omission. Uniqueness
    is on run_at alone for the same reason."""

    run_at = models.DateTimeField(unique=True)
    evaluated = models.PositiveIntegerField()
    cancelled = models.PositiveIntegerField()
    failed = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-run_at"]

    def __str__(self):
        return f"auto-cancel cycle {self.run_at.isoformat()}"


class OrderCycleFailure(models.Model):
    """One row per Order whose per-order auto-cancel call raised during
    a cycle -- not one row per routine already-resolved skip (an order
    found already confirmed/cancelled by the time this cycle reached it
    is not a failure).

    Deliberately not a ForeignKey to Order, mirroring
    CommissionCycleFailure/WithdrawalCycleFailure's own reasoning: the
    audit record must survive even if the underlying Order row is later
    deleted. OrderAdmin.has_delete_permission does return False like its
    siblings -- but direct-shell/ORM deletion of Order rows outside that
    admin UI is a real, already-documented practice in this project (see
    CLAUDE.md's Task 24 production smoke-test cleanup), so this stays a
    plain int, not a real FK, regardless of the admin-UI lockdown."""

    cycle_run = models.ForeignKey(
        OrderCycleRun, on_delete=models.CASCADE, related_name="failures"
    )
    order_id = models.PositiveIntegerField()
    error = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order_id"]

    def __str__(self):
        return f"order_id={self.order_id}"
