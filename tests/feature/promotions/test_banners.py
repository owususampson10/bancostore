import datetime
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone

import pytest

from apps.catalog.models import Category, Product
from apps.promotions.models import Banner


def _make_banner(**overrides):
    today = timezone.localdate()
    defaults = {
        "start_date": today - datetime.timedelta(days=1),
        "end_date": today + datetime.timedelta(days=1),
        "link_type": Banner.LinkType.NONE,
    }
    defaults.update(overrides)
    return Banner.objects.create(**defaults)


@pytest.fixture
def category():
    return Category.objects.create(name="Watches", slug="watches")


@pytest.fixture
def product(category):
    return Product.objects.create(
        name="Classic Chrono", category=category, price=Decimal("1500.00")
    )


@pytest.mark.django_db
def test_active_includes_a_banner_whose_window_covers_today():
    banner = _make_banner()

    assert list(Banner.objects.active()) == [banner]


@pytest.mark.django_db
def test_active_excludes_a_banner_that_has_already_expired():
    today = timezone.localdate()
    _make_banner(
        start_date=today - datetime.timedelta(days=10),
        end_date=today - datetime.timedelta(days=1),
    )

    assert list(Banner.objects.active()) == []


@pytest.mark.django_db
def test_active_excludes_a_banner_that_has_not_started_yet():
    today = timezone.localdate()
    _make_banner(
        start_date=today + datetime.timedelta(days=1),
        end_date=today + datetime.timedelta(days=10),
    )

    assert list(Banner.objects.active()) == []


@pytest.mark.django_db
def test_active_includes_a_banner_on_its_exact_start_and_end_date():
    today = timezone.localdate()
    starts_today = _make_banner(
        start_date=today, end_date=today + datetime.timedelta(days=5)
    )
    ends_today = _make_banner(
        start_date=today - datetime.timedelta(days=5), end_date=today
    )

    active_ids = {b.pk for b in Banner.objects.active()}
    assert active_ids == {starts_today.pk, ends_today.pk}


@pytest.mark.django_db
def test_get_link_url_for_a_product_banner(product):
    banner = _make_banner(link_type=Banner.LinkType.PRODUCT, product=product)

    assert banner.get_link_url() == reverse(
        "catalog:product_detail", args=[product.slug]
    )


@pytest.mark.django_db
def test_get_link_url_for_a_category_banner(category):
    banner = _make_banner(link_type=Banner.LinkType.CATEGORY, category=category)

    assert banner.get_link_url() == (
        reverse("catalog:product_list") + f"?category={category.slug}"
    )


@pytest.mark.django_db
def test_get_link_url_for_a_page_banner():
    banner = _make_banner(
        link_type=Banner.LinkType.PAGE, url="https://bancostore.com/about/"
    )

    assert banner.get_link_url() == "https://bancostore.com/about/"


@pytest.mark.django_db
def test_get_link_url_for_a_no_link_banner_is_none():
    banner = _make_banner(link_type=Banner.LinkType.NONE)

    assert banner.get_link_url() is None
