from decimal import Decimal

from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product

# django.contrib.sites' Site.domain drives sitemap.xml's absolute URLs
# (Task 37b migration 0003), independent of the request's own host --
# unlike robots.txt's Sitemap: line, which is request-host-based.
SITE_DOMAIN = "https://bancostore.com"


@pytest.fixture
def category():
    return Category.objects.create(name="Watches", slug="watches")


@pytest.mark.django_db
def test_robots_txt_returns_plain_text_200(client):
    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")


@pytest.mark.django_db
def test_robots_txt_disallows_private_paths(client):
    response = client.get("/robots.txt")
    lines = response.content.decode().splitlines()

    assert "Disallow: /admin-portal/" in lines
    assert "Disallow: /accounts/" in lines
    assert "Disallow: /cart/" in lines
    assert "Disallow: /media/kyc/" in lines
    assert "Disallow: /admin/" in lines
    # Exact distributor sub-paths, not just the broad /distributors/ prefix
    # (a substring check there would pass for any deeper disallow line
    # without proving these specific, important ones are present --
    # CodeRabbit finding on PR #70).
    assert "Disallow: /distributors/dashboard/" in lines
    assert "Disallow: /distributors/withdraw/" in lines
    assert "Disallow: /distributors/withdrawals/" in lines
    assert "Disallow: /distributors/kyc/" in lines


@pytest.mark.django_db
def test_robots_txt_does_not_disallow_distributor_registration_or_login(client):
    """The one place a blanket `Disallow: /distributors/` would have been
    wrong: registration/login are this site's real distributor-acquisition
    funnel pages, not private dashboard content, and must stay crawlable."""
    response = client.get("/robots.txt")
    lines = response.content.decode().splitlines()

    assert "Disallow: /distributors/register/" not in lines
    assert "Disallow: /distributors/login/" not in lines


@pytest.mark.django_db
def test_robots_txt_does_not_disallow_public_media(client):
    """Product/category photos live under /media/products/ and
    /media/categories/ and should stay crawlable for image search --
    only /media/kyc/ (private ID documents) is off-limits."""
    response = client.get("/robots.txt")
    content = response.content.decode()

    assert "Disallow: /media/products/" not in content
    assert "Disallow: /media/categories/" not in content


@pytest.mark.django_db
def test_robots_txt_references_the_sitemap(client):
    response = client.get("/robots.txt")
    content = response.content.decode()

    assert "Sitemap: http://testserver/sitemap.xml" in content


@pytest.mark.django_db
def test_sitemap_xml_returns_200_valid_xml(client):
    response = client.get("/sitemap.xml")

    assert response.status_code == 200
    assert response["Content-Type"] == "application/xml"
    assert response.content.decode().startswith("<?xml")


@pytest.mark.django_db
def test_sitemap_includes_static_pages(client):
    response = client.get("/sitemap.xml")
    content = response.content.decode()

    home_url = f"{SITE_DOMAIN}{reverse('catalog:home')}"
    shop_url = f"{SITE_DOMAIN}{reverse('catalog:product_list')}"
    about_url = f"{SITE_DOMAIN}{reverse('pages:about')}"
    contact_url = f"{SITE_DOMAIN}{reverse('pages:contact')}"
    terms_url = f"{SITE_DOMAIN}{reverse('pages:terms_of_use')}"

    assert f"<loc>{home_url}</loc>" in content
    assert f"<loc>{shop_url}</loc>" in content
    assert f"<loc>{about_url}</loc>" in content
    assert f"<loc>{contact_url}</loc>" in content
    assert f"<loc>{terms_url}</loc>" in content


@pytest.mark.django_db
def test_sitemap_includes_active_product(client, category):
    """Categories have no dedicated detail page (product_list filters by
    ?category=<slug> query param instead), so only products get their own
    sitemap entries -- a query-string URL would contradict the page's own
    canonical tag, which always points to the bare /shop/ path."""
    product = Product.objects.create(
        name="Classic Chrono",
        category=category,
        price=Decimal("1500.00"),
        is_active=True,
    )

    response = client.get("/sitemap.xml")
    content = response.content.decode()

    product_url = (
        f"{SITE_DOMAIN}{reverse('catalog:product_detail', args=[product.slug])}"
    )
    assert f"<loc>{product_url}</loc>" in content


@pytest.mark.django_db
def test_sitemap_excludes_inactive_product(client, category):
    inactive = Product.objects.create(
        name="Discontinued Item",
        category=category,
        price=Decimal("50.00"),
        is_active=False,
    )

    response = client.get("/sitemap.xml")
    content = response.content.decode()

    inactive_url = (
        f"{SITE_DOMAIN}{reverse('catalog:product_detail', args=[inactive.slug])}"
    )
    assert inactive_url not in content
