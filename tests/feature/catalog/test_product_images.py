import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile

import pytest
from PIL import Image

from apps.catalog.models import Category, Product, ProductImage


def _make_uploaded_image(name="photo.jpg", size=(2000, 1600), color="red", fmt="JPEG"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format=fmt)
    buffer.seek(0)
    content_type = "image/jpeg" if fmt == "JPEG" else f"image/{fmt.lower()}"
    return SimpleUploadedFile(name, buffer.read(), content_type=content_type)


@pytest.fixture
def product():
    category = Category.objects.create(name="Watches", slug="watches")
    return Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("1500.00"), stock=10
    )


@pytest.mark.django_db
def test_uploaded_image_is_converted_to_webp(product):
    product_image = ProductImage.objects.create(
        product=product, image=_make_uploaded_image()
    )

    assert product_image.image.name.endswith(".webp")
    with Image.open(product_image.image) as img:
        assert img.format == "WEBP"


@pytest.mark.django_db
def test_oversized_image_is_resized_down(product):
    product_image = ProductImage.objects.create(
        product=product, image=_make_uploaded_image(size=(3000, 2400))
    )

    with Image.open(product_image.image) as img:
        assert max(img.size) <= 1600


@pytest.mark.django_db
def test_resaving_an_existing_product_image_does_not_reprocess_it(product):
    """Editing an unrelated field (e.g. is_primary) on an existing
    ProductImage must not re-run the resize/WebP conversion on an already
    -processed file — that would just re-compress it lossily every save."""
    product_image = ProductImage.objects.create(
        product=product, image=_make_uploaded_image()
    )
    original_name = product_image.image.name

    product_image.is_primary = True
    product_image.save()

    product_image.refresh_from_db()
    assert product_image.image.name == original_name
