import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

import pytest
from PIL import Image

from apps.catalog.models import Category, Product, ProductImage, ProductVariant


def _make_uploaded_image(name="photo.jpg", color="blue"):
    buffer = io.BytesIO()
    Image.new("RGB", (600, 600), color).save(buffer, format="JPEG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/jpeg")


@pytest.fixture
def category():
    return Category.objects.create(name="Wellness", slug="wellness")


@pytest.mark.django_db
def test_detail_renders_all_fields(client, category):
    product = Product.objects.create(
        name="Vitality Pulse Smart Ring",
        category=category,
        description="A pinnacle of modern wellness engineering.",
        price=Decimal("450.00"),
        pv_value=60,
        stock=5,
        is_active=True,
    )
    ProductVariant.objects.create(product=product, name="Colour", value="Black")
    ProductVariant.objects.create(product=product, name="Colour", value="Silver")
    ProductVariant.objects.create(product=product, name="Size", value="Standard")

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Vitality Pulse Smart Ring" in content
    assert "A pinnacle of modern wellness engineering." in content
    assert "GHS 450.00" in content
    assert "Earns 60 PV for distributors" in content
    assert "Wellness" in content
    assert "Black" in content
    assert "Silver" in content
    assert "Standard" in content


@pytest.mark.django_db
def test_in_stock_product_shows_a_real_add_to_cart_action(client, category):
    # Task 17b: this button was an honest disabled stub through Task 8;
    # it's real now that apps.orders.cart exists.
    product = Product.objects.create(
        name="In Stock Item", category=category, price=Decimal("100.00"), stock=1
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    content = response.content.decode()
    assert "Add to Cart" in content
    assert reverse("orders:cart_add", args=[product.pk]) in content
    assert "In Stock" in content


@pytest.mark.django_db
def test_out_of_stock_product_shows_label_and_no_cart_button(client, category):
    product = Product.objects.create(
        name="Sold Out Item", category=category, price=Decimal("100.00"), stock=0
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    content = response.content.decode()
    assert "Out of Stock" in content
    assert "Add to Cart" not in content


@pytest.mark.django_db
def test_inactive_product_404s(client, category):
    product = Product.objects.create(
        name="Hidden Item", category=category, price=Decimal("100.00"), is_active=False
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    assert response.status_code == 404


@pytest.mark.django_db
def test_nonexistent_slug_404s(client):
    response = client.get(reverse("catalog:product_detail", args=["does-not-exist"]))

    assert response.status_code == 404


@pytest.mark.django_db
def test_related_products_same_category_only(client, category):
    other_category = Category.objects.create(name="Perfumes", slug="perfumes")
    product = Product.objects.create(
        name="Main Product", category=category, price=Decimal("100.00")
    )
    related = Product.objects.create(
        name="Related Wellness Item", category=category, price=Decimal("50.00")
    )
    Product.objects.create(
        name="Unrelated Perfume", category=other_category, price=Decimal("50.00")
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    related_products = list(response.context["related_products"])
    assert related_products == [related]


@pytest.mark.django_db
def test_gallery_shows_all_images_with_primary_shown_first(client, category):
    """8c's acceptance criterion is specifically "all photos (primary
    first)" — no prior test rendered a product with more than one image at
    all, so a gallery that dropped secondary photos or picked the wrong
    "main" image would have passed unnoticed."""
    product = Product.objects.create(
        name="Multi-Photo Product", category=category, price=Decimal("100.00")
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

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    content = response.content.decode()
    # main image's native src fallback + its Alpine init state + its own
    # thumbnail's data-url and <img> src + Task 37a's og:image meta tag +
    # Task 37c's Product JSON-LD "image" field
    assert content.count(primary.image.url) == 6
    assert secondary.image.url in content
    # Compare the thumbnails' own data-url attributes, not the raw url's
    # first occurrence anywhere on the page -- the primary photo's url also
    # appears earlier in og:image/JSON-LD/data-initial-image, so comparing
    # raw occurrences would pass even if the thumbnails themselves were
    # rendered in the wrong order.
    primary_thumb_pos = content.index(f'data-url="{primary.image.url}"')
    secondary_thumb_pos = content.index(f'data-url="{secondary.image.url}"')
    assert primary_thumb_pos < secondary_thumb_pos


@pytest.mark.django_db
def test_gallery_thumbnails_are_wired_to_switch_the_main_image(client, category):
    """Regression test for a bug where clicking any thumbnail other than the
    primary image did nothing -- the thumbnails were plain <img> tags with no
    click handler wiring them to the large image at all."""
    product = Product.objects.create(
        name="Multi-Photo Product", category=category, price=Decimal("100.00")
    )
    primary = ProductImage.objects.create(
        product=product,
        image=_make_uploaded_image("primary.jpg", "blue"),
        is_primary=True,
        order=0,
    )
    secondary = ProductImage.objects.create(
        product=product, image=_make_uploaded_image("secondary.jpg", "red"), order=1
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    content = response.content.decode()
    # The large image's src is driven by Alpine state, initialized from a
    # data attribute (never interpolated directly into the x-data JS string).
    assert ':src="activeImage"' in content
    assert 'data-initial-image="' + primary.image.url + '"' in content
    assert 'x-init="activeImage = $el.dataset.initialImage"' in content
    # Every thumbnail (including the primary's own) carries a click handler
    # that updates that same state, and highlights whichever is active.
    assert content.count('@click="activeImage = $el.dataset.url"') == 2
    assert content.count('data-url="' + primary.image.url + '"') == 1
    assert content.count('data-url="' + secondary.image.url + '"') == 1
