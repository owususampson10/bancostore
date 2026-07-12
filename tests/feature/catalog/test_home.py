import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

import pytest
from PIL import Image

from apps.catalog.models import Category, Product, ProductImage


def _make_uploaded_image(name="photo.jpg", color="blue"):
    buffer = io.BytesIO()
    Image.new("RGB", (600, 600), color).save(buffer, format="JPEG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/jpeg")


@pytest.fixture
def category():
    return Category.objects.create(name="Watches", slug="watches")


@pytest.mark.django_db
def test_home_shows_active_featured_products(client, category):
    shown = Product.objects.create(
        name="Classic Chrono",
        category=category,
        price=Decimal("1500.00"),
        is_active=True,
        is_featured=True,
    )
    Product.objects.create(
        name="Inactive Featured",
        category=category,
        price=Decimal("500.00"),
        is_active=False,
        is_featured=True,
    )
    Product.objects.create(
        name="Active Not Featured",
        category=category,
        price=Decimal("200.00"),
        is_active=True,
        is_featured=False,
    )

    response = client.get(reverse("catalog:home"))

    assert response.status_code == 200
    featured = list(response.context["featured_products"])
    assert featured == [shown]
    content = response.content.decode()
    assert "Classic Chrono" in content
    assert "Inactive Featured" not in content
    assert "Active Not Featured" not in content


@pytest.mark.django_db
def test_home_renders_with_no_products(client):
    response = client.get(reverse("catalog:home"))

    assert response.status_code == 200
    assert "No featured products yet" in response.content.decode()


@pytest.mark.django_db
def test_home_shows_category_tiles(client, category):
    Category.objects.create(name="Perfumes", slug="perfumes")

    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert "Watches" in content
    assert "Perfumes" in content


@pytest.mark.django_db
def test_category_tile_shows_photo_when_category_has_one(client):
    with_photo = Category.objects.create(
        name="Watches", slug="watches", image=_make_uploaded_image("watches.jpg")
    )
    without_photo = Category.objects.create(name="Perfumes", slug="perfumes")

    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert with_photo.image.url in content
    # the photo-less category still renders its name, just as a flat tile
    assert without_photo.name in content


@pytest.mark.django_db
def test_home_featured_card_shows_primary_image_and_ghs_price(client, category):
    """8a's acceptance criterion is specifically "primary image, name, GHS
    price" — the earlier test only ever checked the name, so a card
    rendering with no image or a malformed price would have passed
    unnoticed (this is exactly how the missing-categories bug shipped)."""
    product = Product.objects.create(
        name="Classic Chrono",
        category=category,
        price=Decimal("1500.00"),
        is_active=True,
        is_featured=True,
    )
    secondary = ProductImage.objects.create(
        product=product, image=_make_uploaded_image("secondary.jpg", "red"), order=1
    )
    primary = ProductImage.objects.create(
        product=product,
        image=_make_uploaded_image("primary.jpg", "blue"),
        is_primary=True,
        order=0,
    )

    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert primary.image.url in content
    assert secondary.image.url not in content
    assert "GHS 1,500.00" in content
