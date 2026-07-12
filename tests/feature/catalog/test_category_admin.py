import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

import pytest
from PIL import Image

from apps.catalog.models import Category


def _make_uploaded_image(name="photo.jpg", size=(2000, 1600), color="red"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/jpeg")


@pytest.mark.django_db
def test_staff_can_create_a_category_via_django_admin(staff_client):
    response = staff_client.post(
        reverse("admin:catalog_category_add"),
        {"name": "Watches", "slug": "watches"},
    )

    assert response.status_code == 302
    category = Category.objects.get(name="Watches")
    assert category.slug == "watches"


@pytest.mark.django_db
def test_category_image_is_resized_and_converted_to_webp():
    category = Category.objects.create(
        name="Watches", slug="watches", image=_make_uploaded_image(size=(3000, 2400))
    )

    assert category.image.name.endswith(".webp")
    with Image.open(category.image) as img:
        assert img.format == "WEBP"
        assert max(img.size) <= 1600


@pytest.mark.django_db
def test_category_without_image_has_falsy_image_field():
    category = Category.objects.create(name="Perfumes", slug="perfumes")

    assert not category.image
