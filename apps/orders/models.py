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

    payment_reference = models.CharField(max_length=100, unique=True)
    # code-review-and-quality (2026-07-24): no index yet, even though
    # OrderAdmin already filters on this column and WithdrawalRequest
    # (Task 16b, same task shape) pre-emptively indexed its own status
    # column for this project's "hundreds of thousands of users" scale
    # reasoning. Not added here since no real query shape exists yet
    # (17c/Task 18 will define it) -- a composite index guessed now could
    # easily be the wrong one. Revisit once 17c/Task 18's actual queries
    # (e.g. "pending orders older than N hours" for auto-cancel) exist.
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Money- and PII-adjacent (guest checkout stores raw contact info with
    # no account) -- SPEC.md's "log admin actions affecting money ... via
    # django-simple-history" applies directly, matching
    # WithdrawalRequest/Distributor's own precedent.
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(subtotal__gte=0)
                    & models.Q(delivery_fee__gte=0)
                    & models.Q(total__gte=0)
                    & models.Q(total=models.F("subtotal") + models.F("delivery_fee"))
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

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=models.Q(quantity__gte=1) & models.Q(unit_price__gte=0),
                name="order_item_quantity_and_price_sane",
            ),
        ]

    def __str__(self):
        return (
            f"OrderItem<order={self.order_id} "
            f"product={self.product_name} x{self.quantity}>"
        )
