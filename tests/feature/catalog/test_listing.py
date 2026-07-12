from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product


@pytest.fixture
def watches():
    return Category.objects.create(name="Watches", slug="watches")


@pytest.fixture
def perfumes():
    return Category.objects.create(name="Perfumes", slug="perfumes")


def _product(category, **kwargs):
    defaults = {"category": category, "price": Decimal("100.00"), "is_active": True}
    defaults.update(kwargs)
    return Product.objects.create(**defaults)


@pytest.mark.django_db
def test_search_returns_matching_product(client, watches, perfumes):
    _product(watches, name="Classic Chrono Gold")
    _product(perfumes, name="Oud Elegance")

    response = client.get(reverse("catalog:product_list"), {"q": "Chrono"})

    names = [p.name for p in response.context["page_obj"]]
    assert names == ["Classic Chrono Gold"]


@pytest.mark.django_db
def test_category_filter_narrows_results(client, watches, perfumes):
    _product(watches, name="Classic Chrono Gold")
    _product(perfumes, name="Oud Elegance")

    response = client.get(reverse("catalog:product_list"), {"category": "watches"})

    names = [p.name for p in response.context["page_obj"]]
    assert names == ["Classic Chrono Gold"]


@pytest.mark.django_db
def test_search_category_and_sort_combine(client, watches, perfumes):
    cheap = _product(watches, name="Watch Basic", price=Decimal("100.00"))
    expensive = _product(watches, name="Watch Deluxe", price=Decimal("900.00"))
    _product(perfumes, name="Watch Perfume Tie-in", price=Decimal("50.00"))

    response = client.get(
        reverse("catalog:product_list"),
        {"q": "Watch", "category": "watches", "sort": "price_desc"},
    )

    names = [p.name for p in response.context["page_obj"]]
    assert names == [expensive.name, cheap.name]


@pytest.mark.django_db
def test_inactive_products_never_shown(client, watches):
    _product(watches, name="Hidden Product", is_active=False)

    response = client.get(reverse("catalog:product_list"))

    assert response.context["page_obj"].paginator.count == 0


@pytest.mark.django_db
def test_out_of_stock_product_shows_label(client, watches):
    _product(watches, name="Sold Out Item", stock=0)

    response = client.get(reverse("catalog:product_list"))

    content = response.content.decode()
    assert "Out of Stock" in content


@pytest.mark.django_db
def test_page_two_preserves_active_filters(client, watches, perfumes):
    for i in range(10):
        _product(watches, name=f"Watch {i}")
    _product(perfumes, name="Unrelated Perfume")

    response = client.get(
        reverse("catalog:product_list"), {"category": "watches", "page": 2}
    )

    names = [p.name for p in response.context["page_obj"]]
    assert len(names) == 1
    assert all(name.startswith("Watch") for name in names)


@pytest.mark.django_db
def test_htmx_request_returns_partial_without_header_or_footer(client, watches):
    _product(watches, name="Any Product")

    response = client.get(reverse("catalog:product_list"), HTTP_HX_REQUEST="true")

    content = response.content.decode()
    assert "Any Product" in content
    assert "Become a Distributor" not in content


@pytest.mark.django_db
def test_full_page_declares_hx_push_url_so_filtered_results_stay_shareable(client):
    """8b's acceptance criterion explicitly names hx-push-url as the
    mechanism that keeps filtered results bookmarkable — assert the
    attribute is actually on the page, not just that the feature "feels"
    reactive when clicked once during a manual check."""
    response = client.get(reverse("catalog:product_list"))

    assert 'hx-push-url="true"' in response.content.decode()


@pytest.mark.django_db
def test_grid_query_count_does_not_grow_with_product_count(client, watches):
    """Compares query count at two product counts rather than asserting an
    absolute number — see test_product_admin.py's
    test_product_changelist_does_not_n_plus_one_on_category for why (silk's
    middleware, active locally under DEBUG=True, adds its own DB writes on
    every request and would make a fixed threshold flaky). Same tolerance
    of 2 for the same reason."""
    from django.db import reset_queries

    url = reverse("catalog:product_list")

    for i in range(3):
        _product(watches, name=f"Small batch {i}")
    reset_queries()
    with CaptureQueriesContext(connection) as small_batch:
        client.get(url)

    for i in range(20):
        _product(watches, name=f"Large batch {i}")
    reset_queries()
    with CaptureQueriesContext(connection) as large_batch:
        client.get(url)

    assert len(large_batch.captured_queries) <= len(small_batch.captured_queries) + 2
