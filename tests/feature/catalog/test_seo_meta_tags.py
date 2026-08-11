import io
import json
import re
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

import pytest
from PIL import Image

from apps.catalog.models import Category, Product, ProductImage
from apps.pages.models import SocialMediaLink

_JSON_LD_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL
)


def _make_uploaded_image(name="photo.jpg", color="blue"):
    buffer = io.BytesIO()
    Image.new("RGB", (600, 600), color).save(buffer, format="JPEG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/jpeg")


def _json_ld_blocks(content):
    return [json.loads(match) for match in _JSON_LD_RE.findall(content)]


@pytest.fixture
def category():
    return Category.objects.create(name="Watches", slug="watches")


@pytest.mark.django_db
def test_home_page_has_real_title_and_meta_description(client):
    response = client.get(reverse("catalog:home"))
    content = response.content.decode()

    assert (
        "<title>Bancostore | Shop Quality Products & Become a Distributor in "
        "Ghana</title>" in content
    )
    assert (
        'name="description" content="Shop watches, jewellery, and perfumes at '
        "Bancostore" in content
    )


@pytest.mark.django_db
def test_home_page_has_canonical_and_open_graph_tags(client):
    response = client.get(reverse("catalog:home"))
    content = response.content.decode()

    assert '<link rel="canonical" href="http://testserver/">' in content
    assert 'property="og:site_name" content="Bancostore"' in content
    assert 'property="og:type" content="website"' in content
    assert 'property="og:url" content="http://testserver/">' in content
    assert (
        'property="og:image" content="http://testserver/static/'
        'bancostore-brand/social/og-default.png">' in content
    )
    assert 'property="og:image:width" content="1200">' in content
    assert 'property="og:image:height" content="630">' in content
    assert 'name="twitter:card" content="summary_large_image">' in content
    # Twitter/X falls back to og:title/og:description/og:image when these
    # aren't set separately -- deliberately not duplicated.
    assert "twitter:title" not in content
    assert "twitter:description" not in content


@pytest.mark.django_db
def test_product_detail_open_graph_uses_product_image_and_type(client, category):
    product = Product.objects.create(
        name="Classic Chrono",
        category=category,
        price=Decimal("1500.00"),
        description="A timeless watch.",
        is_active=True,
    )
    image = ProductImage.objects.create(
        product=product, image=_make_uploaded_image(), is_primary=True
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))
    content = response.content.decode()

    assert 'property="og:type" content="product">' in content
    assert (
        f'property="og:image" content="http://testserver{image.image.url}">' in content
    )
    assert "<title>Classic Chrono | Bancostore</title>" in content


@pytest.mark.django_db
def test_product_detail_open_graph_falls_back_to_default_image_when_no_photo(
    client, category
):
    product = Product.objects.create(
        name="No Photo Yet",
        category=category,
        price=Decimal("99.00"),
        description="Coming soon.",
        is_active=True,
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))
    content = response.content.decode()

    assert (
        'property="og:image" content="http://testserver/static/'
        'bancostore-brand/social/og-default.png">' in content
    )


@pytest.mark.django_db
def test_home_page_has_organization_and_website_json_ld(client):
    response = client.get(reverse("catalog:home"))
    content = response.content.decode()

    blocks = _json_ld_blocks(content)
    assert len(blocks) == 1
    graph = blocks[0]["@graph"]
    types = {entry["@type"] for entry in graph}
    assert types == {"Organization", "WebSite"}

    organization = next(e for e in graph if e["@type"] == "Organization")
    assert organization["name"] == "Bancostore"
    assert organization["url"] == "http://testserver/"
    assert organization["logo"].endswith("bancostore-logo-orange.svg")

    website = next(e for e in graph if e["@type"] == "WebSite")
    search_target = website["potentialAction"]["target"]["urlTemplate"]
    assert search_target == "http://testserver/shop/?q={search_term_string}"


@pytest.mark.django_db
def test_product_detail_has_product_json_ld_with_offer_and_image(client, category):
    product = Product.objects.create(
        name="Classic Chrono",
        category=category,
        price=Decimal("1500.00"),
        description="A timeless watch.",
        stock=3,
        is_active=True,
    )
    image = ProductImage.objects.create(
        product=product, image=_make_uploaded_image(), is_primary=True
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))
    content = response.content.decode()

    blocks = _json_ld_blocks(content)
    product_block = next(b for b in blocks if b.get("@type") == "Product")

    assert product_block["name"] == "Classic Chrono"
    assert product_block["description"] == "A timeless watch."
    assert product_block["image"] == f"http://testserver{image.image.url}"
    offer = product_block["offers"]
    assert offer["priceCurrency"] == "GHS"
    assert offer["price"] == "1500.00"
    assert offer["availability"] == "https://schema.org/InStock"


@pytest.mark.django_db
def test_product_detail_json_ld_marks_out_of_stock_and_omits_image_when_no_photo(
    client, category
):
    product = Product.objects.create(
        name="No Photo Yet",
        category=category,
        price=Decimal("99.00"),
        description="Coming soon.",
        stock=0,
        is_active=True,
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))
    content = response.content.decode()

    blocks = _json_ld_blocks(content)
    product_block = next(b for b in blocks if b.get("@type") == "Product")

    assert "image" not in product_block
    assert product_block["offers"]["availability"] == "https://schema.org/OutOfStock"


@pytest.mark.django_db
def test_product_json_ld_escapes_closing_script_tag_in_description(client, category):
    """CodeRabbit finding on PR #70: json.dumps() alone leaves a literal
    "</script>" unescaped, so a product description containing that exact
    sequence could break out of the <script type="application/ld+json">
    element and inject arbitrary HTML. Proves both halves: the raw HTML
    never contains an unescaped closing tag mid-script, and the escaped
    JSON still round-trips back to the real description."""
    malicious_description = "Great watch.</script><script>alert(1)</script>"
    product = Product.objects.create(
        name="Escape Test Watch",
        category=category,
        price=Decimal("200.00"),
        description=malicious_description,
        is_active=True,
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))
    content = response.content.decode()

    ld_script_start = content.index('<script type="application/ld+json">')
    ld_script_end = content.index("</script>", ld_script_start)
    raw_json_ld_text = content[ld_script_start:ld_script_end]
    assert "</script>" not in raw_json_ld_text
    assert "<script>alert(1)" not in raw_json_ld_text

    blocks = _json_ld_blocks(content)
    product_block = next(b for b in blocks if b.get("@type") == "Product")
    assert product_block["description"] == malicious_description


@pytest.mark.django_db
def test_organization_json_ld_escapes_closing_script_tag_in_social_link_url(client):
    """Same CodeRabbit-flagged escaping gap, fixed in both JSON-LD builders
    -- proven here for organization_json_ld's sameAs list, sourced from
    admin-entered SocialMediaLink.url (Task 35), not just the Product twin
    CodeRabbit's comment specifically pointed at. .objects.create()
    bypasses URLField's own form-level validation (only enforced via
    ModelForm/full_clean, not on save()), which is fine here -- the point
    is proving dumps_for_script_tag's escaping holds regardless of what
    reaches it, not re-testing URLField's validator."""
    SocialMediaLink.objects.create(
        name="Evil",
        url="https://example.com/evil</script><script>alert(1)</script>",
        platform="custom",
        icon_color="#123456",
    )

    response = client.get(reverse("catalog:home"))
    content = response.content.decode()

    ld_script_start = content.index('<script type="application/ld+json">')
    ld_script_end = content.index("</script>", ld_script_start)
    raw_json_ld_text = content[ld_script_start:ld_script_end]
    assert "</script>" not in raw_json_ld_text
    assert "<script>alert(1)" not in raw_json_ld_text
