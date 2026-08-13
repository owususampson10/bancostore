from decimal import ROUND_HALF_UP, Decimal

from django.core.files.uploadedfile import UploadedFile
from django.db import models
from django.urls import reverse
from django.utils import timezone

from bancostore.media import resize_and_convert_to_webp


class BannerQuerySet(models.QuerySet):
    def active(self):
        """Task 42: "no admin action needed on expiry" -- a banner simply
        stops matching this filter once end_date has passed, and starts
        matching once start_date arrives. Both bounds inclusive (a banner
        still shows in full on its own start_date and end_date)."""
        today = timezone.localdate()
        return self.filter(start_date__lte=today, end_date__gte=today)


class Banner(models.Model):
    """Task 42 (SPEC_PHASE2.md Feature 1 / source doc Section 11.2). Admin
    uploads a banner image that links to a product, a category, an
    arbitrary page/URL, or nowhere at all, shown on the home page only
    while today falls inside [start_date, end_date]."""

    class LinkType(models.TextChoices):
        PRODUCT = "product", "Product"
        CATEGORY = "category", "Category"
        PAGE = "page", "Custom URL"
        NONE = "none", "No link"

    image = models.ImageField(upload_to="banners/")
    link_type = models.CharField(
        max_length=10, choices=LinkType.choices, default=LinkType.NONE
    )
    # SET_NULL, not CASCADE/PROTECT: a deleted Product/Category shouldn't
    # take an otherwise-fine banner down with it -- get_link_url() already
    # treats a missing target as "no link" via its own product_id/
    # category_id checks below, matching the LinkType.NONE case exactly.
    product = models.ForeignKey(
        "catalog.Product",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="banners",
    )
    category = models.ForeignKey(
        "catalog.Category",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="banners",
    )
    url = models.URLField(blank=True, default="")
    start_date = models.DateField()
    end_date = models.DateField()
    order = models.PositiveIntegerField(
        default=0, help_text="Lower numbers show first."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = BannerQuerySet.as_manager()

    class Meta:
        ordering = ["order", "-created_at"]

    def __str__(self):
        return f"Banner #{self.pk or '(unsaved)'} ({self.get_link_type_display()})"

    def save(self, *args, **kwargs):
        if self.image and isinstance(self.image.file, UploadedFile):
            self.image = resize_and_convert_to_webp(self.image)
        super().save(*args, **kwargs)

    def get_link_url(self):
        if self.link_type == self.LinkType.PRODUCT and self.product_id:
            return reverse("catalog:product_detail", args=[self.product.slug])
        if self.link_type == self.LinkType.CATEGORY and self.category_id:
            return reverse("catalog:product_list") + f"?category={self.category.slug}"
        if self.link_type == self.LinkType.PAGE and self.url:
            return self.url
        return None


class DiscountCode(models.Model):
    """Task 43a (SPEC_PHASE2.md Feature 2 / source doc Section 11.1). Admin
    creates a fixed-amount or percentage-off code with an expiry date, an
    audience restriction, and a usage limit; a customer enters it at
    checkout (Task 43b) and the discount applies immediately.

    Usage-limit design confirmed directly with the user 2026-08-13, not
    read out of the source doc -- Section 11.1 only ever describes a
    global cap ("this code can only be used 50 times total"). Modeled as
    TWO independent fields instead, matching how Shopify/WooCommerce/
    Stripe all handle this in real e-commerce systems: max_uses (the
    global cap the source doc describes) plus limit_one_per_customer (a
    separate on/off toggle layered on top, default on -- the real-world
    default for most single-use promo codes)."""

    class DiscountType(models.TextChoices):
        FIXED = "fixed", "Fixed Amount"
        PERCENTAGE = "percentage", "Percentage"

    class Audience(models.TextChoices):
        RETAIL = "retail", "Retail Customers Only"
        DISTRIBUTOR = "distributor", "Distributors Only"
        EVERYONE = "everyone", "Everyone"

    code = models.CharField(max_length=32, unique=True)
    discount_type = models.CharField(max_length=10, choices=DiscountType.choices)
    # GHS amount when discount_type=FIXED, a 0-100 percentage when
    # discount_type=PERCENTAGE -- validated against the right range for
    # each type in DiscountCodeForm.clean() (apps/admin_portal/forms.py),
    # not here, matching Banner's own "form is the real enforcement"
    # convention for a field whose valid range depends on a sibling field.
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    expiry_date = models.DateField()
    audience = models.CharField(
        max_length=15, choices=Audience.choices, default=Audience.EVERYONE
    )
    max_uses = models.PositiveIntegerField(help_text="Global cap across all customers.")
    # Incremented atomically at redemption time (Task 43b), the same
    # conditional-update discipline as Product.stock (Task 7) -- never a
    # naive read-then-write, since two concurrent checkouts could
    # otherwise both read "not yet exhausted" and both succeed.
    times_used = models.PositiveIntegerField(default=0)
    limit_one_per_customer = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.code

    def save(self, *args, **kwargs):
        # Case-insensitive in practice (matching Shopify/WooCommerce/
        # Stripe): normalized to uppercase once here so every later
        # lookup (checkout redemption, the unique constraint itself) never
        # has to guess at or reconcile casing.
        if self.code:
            self.code = self.code.upper()
        super().save(*args, **kwargs)

    def calculate_discount(self, subtotal):
        """Never returns more than `subtotal` itself -- a discount can
        reduce an order to GHS 0, never negative. Rounded to the pesewa
        (Decimal.quantize(..., ROUND_HALF_UP)), matching this codebase's
        one established money-rounding convention (apps/commissions)."""
        if self.discount_type == self.DiscountType.PERCENTAGE:
            raw = subtotal * (self.amount / Decimal("100"))
        else:
            raw = self.amount
        capped = min(raw, subtotal)
        return capped.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def is_redeemable(self):
        """Active, not yet past its expiry date, and still has room under
        its global usage cap -- the three checks that don't depend on
        WHO is checking out (audience and per-customer-limit are Task
        43b/43c's job, since those need the specific customer)."""
        if not self.is_active:
            return False
        if self.expiry_date < timezone.localdate():
            return False
        if self.times_used >= self.max_uses:
            return False
        return True
