import io
from pathlib import Path

from django.core.files.base import ContentFile

from PIL import Image

# Generous cap on the longest edge — display code constrains further with
# CSS/responsive images; this just keeps original upload sizes (often
# several MB straight off a phone camera) from being stored and served as-is.
MAX_IMAGE_DIMENSION = 1600
WEBP_QUALITY = 85


def resize_and_convert_to_webp(image_field):
    """Shared across every model with a user-uploaded photo (Category.image,
    ProductImage.image since Task 7; Distributor's KYC images since Task
    11a) — every freshly-uploaded photo is resized and converted to WebP
    in-process (Django Admin's file widget has no async processing step of
    its own)."""
    img = Image.open(image_field)
    img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    buffer = io.BytesIO()
    img.save(buffer, format="WEBP", quality=WEBP_QUALITY)
    buffer.seek(0)

    original_stem = Path(image_field.name).stem
    return ContentFile(buffer.read(), name=f"{original_stem}.webp")
