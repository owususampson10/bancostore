import io
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile
from django.db import models
from django.utils.text import slugify

from PIL import Image

# Generous cap on the longest edge — the storefront (Task 8) will further
# constrain display size with CSS/responsive images; this just keeps
# original upload sizes (often several MB straight off a phone camera) from
# being stored and served as-is.
MAX_IMAGE_DIMENSION = 1600
WEBP_QUALITY = 85


def _resize_and_convert_to_webp(image_field):
    """Shared by ProductImage and Category.image — every freshly-uploaded
    photo is resized and converted to WebP in-process (Django Admin's file
    widget has no async processing step of its own)."""
    img = Image.open(image_field)
    img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    buffer = io.BytesIO()
    img.save(buffer, format="WEBP", quality=WEBP_QUALITY)
    buffer.seek(0)

    original_stem = Path(image_field.name).stem
    return ContentFile(buffer.read(), name=f"{original_stem}.webp")


class Category(models.Model):
    """Product categories (Watches, Jewellery, Perfumes, etc.) — admin-managed
    via Django Admin so new categories can be added without code changes."""

    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    image = models.ImageField(
        upload_to="categories/",
        blank=True,
        help_text="Shown on the storefront's category tile (Task 8). Optional — "
        "categories without one render as a plain text tile.",
    )

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        # Same freshly-uploaded-vs-already-saved guard as ProductImage below
        # — avoids reprocessing (and double-compressing) on every unrelated
        # field edit.
        if self.image and isinstance(self.image.file, UploadedFile):
            self.image = _resize_and_convert_to_webp(self.image)
        super().save(*args, **kwargs)


class ProductQuerySet(models.QuerySet):
    def storefront_visible(self):
        """Only ever show active products publicly (Task 8) — every
        storefront view needs this exact filter/select_related/
        prefetch_related combination, so it's named once here rather than
        repeated at each call site, where it would be easy to forget the
        is_active=True filter on a new page."""
        return (
            self.filter(is_active=True)
            .select_related("category")
            .prefetch_related("images")
        )


class Product(models.Model):
    """SPEC.md Section 1.2/1.4 — price is what a regular customer pays;
    pv_value is the separate Point Value credited toward the binary tree
    only when a distributor buys it (Section 1.4), never for regular
    customer purchases."""

    objects = ProductQuerySet.as_manager()

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True, blank=True)
    description = models.TextField(blank=True)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="products"
    )
    price = models.DecimalField(max_digits=10, decimal_places=2)
    pv_value = models.PositiveIntegerField(
        default=0, help_text="Point Value credited to distributors only."
    )
    stock = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    @property
    def primary_image(self):
        """Task 8's storefront always shows the primary image first. Reads
        from the already-prefetched `images` queryset (list()/iteration,
        never .first()/.filter()) so this never adds an extra query on top
        of a view's own prefetch_related("images")."""
        images = list(self.images.all())
        for image in images:
            if image.is_primary:
                return image
        return images[0] if images else None

    @property
    def in_stock(self):
        """Out-of-stock behaviour is deliberately hardcoded to "show the
        product with an Out of Stock label" (docs/Bancostore_Features_and_
        Workflow_v4.docx Section 13.10's stated default), not a
        constance-editable setting — Section 13.10 is explicitly out of MVP
        scope per SPEC.md ("stubbed with sane hardcoded defaults and
        revisited in the next phase"). Task 8's storefront will use this
        property to decide whether to render "Add to Cart" or "Out of
        Stock" instead of hiding/backordering the product."""
        return self.stock > 0


class ProductImage(models.Model):
    """Multiple photos per product (SPEC.md Section 1.2: "Multiple clear
    photos of the product"). Every freshly-uploaded image is resized and
    converted to WebP on save — Pillow does this in-process since Django
    Admin's file widget has no async processing step of its own."""

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="images"
    )
    image = models.ImageField(upload_to="products/")
    is_primary = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"Image for {self.product.name}"

    def save(self, *args, **kwargs):
        # self.image.file is an UploadedFile (InMemoryUploadedFile/
        # TemporaryUploadedFile) only for a freshly-assigned upload — an
        # already-saved image loaded back from storage is a plain FieldFile,
        # which is NOT an UploadedFile. This avoids reprocessing (and
        # double-compressing) the image on every unrelated field edit.
        if self.image and isinstance(self.image.file, UploadedFile):
            self.image = _resize_and_convert_to_webp(self.image)
        super().save(*args, **kwargs)


class ProductVariant(models.Model):
    """Optional variant attributes (e.g. Colour: Red, Size: Large) — SPEC.md
    Section 1.2. Kept as simple name/value pairs for this task's scope;
    stock is tracked at the Product level only, not per variant (Task 7's
    own acceptance criteria only covers Product-level stock — full variant
    stock tracking, if ever needed, is a separate future task)."""

    product = models.ForeignKey(
        Product, on_delete=models.CASCADE, related_name="variants"
    )
    name = models.CharField(max_length=50, help_text="e.g. Colour, Size")
    value = models.CharField(max_length=50, help_text="e.g. Red, Large")

    class Meta:
        ordering = ["name", "value"]

    def __str__(self):
        return f"{self.name}: {self.value}"
