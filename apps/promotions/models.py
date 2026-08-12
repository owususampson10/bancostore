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
