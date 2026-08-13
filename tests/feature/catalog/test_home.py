import datetime
import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

import pytest
from PIL import Image

from apps.catalog.models import Category, Product, ProductImage
from apps.promotions.models import Banner


def _make_uploaded_image(name="photo.jpg", color="blue"):
    buffer = io.BytesIO()
    Image.new("RGB", (600, 600), color).save(buffer, format="JPEG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/jpeg")


def _banner_anchor_tag(content, image_url):
    """CodeRabbit finding (PR #74): asserting href and aria-label
    independently against the whole page only proves both strings exist
    SOMEWHERE, not that they're on the SAME anchor -- a broken href on
    one banner could pass if a different banner's aria-label happened to
    match. Returns the specific <a ...> opening tag that wraps this
    banner's own image, so both attributes can be checked together."""
    img_pos = content.index(image_url)
    anchor_start = content.rindex("<a ", 0, img_pos)
    anchor_end = content.index(">", anchor_start)
    return content[anchor_start : anchor_end + 1]


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
def test_category_bento_grid_renders_every_tile_at_five_or_more_categories(client):
    """Code-review finding on Task 31: the bento grid's first tile spans
    2x2, which used to exactly fill a fixed 8-cell (4-col x 2-row) grid
    at 4 categories + the trailing "View All Products" tile -- any real
    category count of 5+ pushed a tile into an unsized, visually broken
    row. This doesn't assert on CSS layout directly (out of reach for a
    server-rendered HTML test), but confirms every category still
    renders with no template error or silent truncation regardless of
    count, guarding the underlying data loop while the CSS fix itself
    was verified live in a real browser."""
    names = ["Watches", "Jewellery", "Perfumes", "Wellness", "Home & Living", "Bags"]
    for name in names:
        Category.objects.create(
            name=name, slug=name.lower().replace(" & ", "-"), image=None
        )

    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    for name in names:
        # Django's autoescaping turns "&" into "&amp;" in rendered HTML.
        assert name.replace("&", "&amp;") in content
    # Code-review finding: assert the real link, not just the label text --
    # a "View All Products" string rendered as plain (non-link) text would
    # have passed the old, weaker assertion.
    product_list_url = reverse("catalog:product_list")
    assert f'<a href="{product_list_url}"' in content
    assert "View All Products" in content


@pytest.mark.django_db
def test_category_bento_grid_second_tile_wide_and_view_all_categories_outside_grid(
    client,
):
    """Direct user feedback on Task 31's bento build: the second category
    must also get a wide `md:col-span-2` tile (matching the real Stitch
    mockup's top-right tile), not just the first category's large 2x2
    tile -- and "View All Categories" must be a separate link outside the
    grid, distinct from the in-grid "View All Products" fallback tile."""
    # Category.Meta.ordering is ["name"] -- name these alphabetically so
    # creation order matches the real display order the view queries.
    first = Category.objects.create(name="A Watches", slug="watches")
    second = Category.objects.create(name="B Jewellery", slug="jewellery")
    Category.objects.create(name="C Perfumes", slug="perfumes")

    response = client.get(reverse("catalog:home"))

    content = response.content.decode()

    def tile_class(category_slug):
        href_pos = content.index(f'?category={category_slug}"')
        tag_end = content.index(">", href_pos)
        tag = content[href_pos:tag_end]
        class_start = tag.index('class="') + len('class="')
        class_end = tag.index('"', class_start)
        return tag[class_start:class_end]

    first_tile_class = tile_class(first.slug)
    assert "md:col-span-2 md:row-span-2" in first_tile_class

    second_tile_class = tile_class(second.slug)
    assert "md:col-span-2" in second_tile_class
    assert "md:row-span-2" not in second_tile_class

    assert "View All Categories" in content
    assert "View All Products" in content
    product_list_url = reverse("catalog:product_list")
    # Both labels link to the same real destination, but as two distinct
    # anchor elements (one inside the grid, one outside it) -- not one
    # link reused/duplicated by accident.
    assert content.count(f'<a href="{product_list_url}"') >= 2


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


@pytest.mark.django_db
def test_home_shows_only_currently_active_banners(client):
    today = timezone.localdate()
    active = Banner.objects.create(
        image=_make_uploaded_image("active.jpg"),
        start_date=today - datetime.timedelta(days=1),
        end_date=today + datetime.timedelta(days=1),
    )
    Banner.objects.create(
        image=_make_uploaded_image("expired.jpg"),
        start_date=today - datetime.timedelta(days=10),
        end_date=today - datetime.timedelta(days=1),
    )
    Banner.objects.create(
        image=_make_uploaded_image("upcoming.jpg"),
        start_date=today + datetime.timedelta(days=1),
        end_date=today + datetime.timedelta(days=10),
    )

    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    assert active.image.url in content
    banners = list(response.context["active_banners"])
    assert banners == [active]


@pytest.mark.django_db
def test_home_renders_with_no_active_banners(client):
    response = client.get(reverse("catalog:home"))

    assert response.status_code == 200


@pytest.mark.django_db
def test_home_banner_links_to_its_product(client, category):
    product = Product.objects.create(
        name="Classic Chrono", category=category, price=Decimal("1500.00")
    )
    today = timezone.localdate()
    banner = Banner.objects.create(
        image=_make_uploaded_image("banner.jpg"),
        link_type=Banner.LinkType.PRODUCT,
        product=product,
        start_date=today,
        end_date=today,
    )

    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    expected_url = reverse("catalog:product_detail", args=[product.slug])
    anchor = _banner_anchor_tag(content, banner.image.url)
    assert f'href="{expected_url}"' in anchor
    assert 'aria-label="View Classic Chrono"' in anchor


@pytest.mark.django_db
def test_home_banner_link_has_an_accessible_name_for_each_link_type(client, category):
    """CodeRabbit finding (PR #74): the linked banner's only child was an
    <img alt="">, so a screen reader announced the link with no
    accessible name at all -- no way to tell what the promotion was or
    where it led. Covers the category and custom-URL cases the earlier
    product-only test didn't."""
    today = timezone.localdate()
    category_banner = Banner.objects.create(
        image=_make_uploaded_image("category-banner.jpg"),
        link_type=Banner.LinkType.CATEGORY,
        category=category,
        start_date=today,
        end_date=today,
        order=0,
    )
    page_banner = Banner.objects.create(
        image=_make_uploaded_image("page-banner.jpg"),
        link_type=Banner.LinkType.PAGE,
        url="https://bancostore.com/about/",
        start_date=today,
        end_date=today,
        order=1,
    )

    response = client.get(reverse("catalog:home"))

    content = response.content.decode()
    expected_category_url = (
        reverse("catalog:product_list") + f"?category={category.slug}"
    )
    category_anchor = _banner_anchor_tag(content, category_banner.image.url)
    assert f'href="{expected_category_url}"' in category_anchor
    assert f'aria-label="Shop {category.name}"' in category_anchor

    page_anchor = _banner_anchor_tag(content, page_banner.image.url)
    assert 'href="https://bancostore.com/about/"' in page_anchor
    assert 'aria-label="View this promotion"' in page_anchor
