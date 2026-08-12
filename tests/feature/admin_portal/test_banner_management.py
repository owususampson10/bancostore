import datetime
import io
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

import pytest
from PIL import Image

from apps.catalog.models import Category, Product
from apps.promotions.models import Banner


def _make_uploaded_image(name="banner.jpg"):
    buffer = io.BytesIO()
    Image.new("RGB", (1200, 400), "blue").save(buffer, format="JPEG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/jpeg")


def _valid_payload(**overrides):
    today = datetime.date.today()
    payload = {
        "link_type": Banner.LinkType.NONE,
        "product": "",
        "category": "",
        "url": "",
        "start_date": today.isoformat(),
        "end_date": (today + datetime.timedelta(days=7)).isoformat(),
        "order": "0",
    }
    payload.update(overrides)
    return payload


def _list_url():
    return reverse("admin_portal:catalog_banner_list")


def _create_url():
    return reverse("admin_portal:catalog_banner_create")


def _edit_url(banner):
    return reverse("admin_portal:catalog_banner_edit", args=[banner.pk])


def _delete_url(banner):
    return reverse("admin_portal:catalog_banner_delete", args=[banner.pk])


@pytest.fixture
def category():
    return Category.objects.create(name="Watches", slug="watches")


@pytest.fixture
def product(category):
    return Product.objects.create(
        name="Classic Chrono", category=category, price=Decimal("1500.00")
    )


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_non_staff_user_is_forbidden_from_the_banner_list(client, db):
    response = client.get(_list_url())
    assert response.status_code in (302, 403)


@pytest.mark.django_db
def test_banner_list_shows_an_empty_state_with_no_banners(staff_client):
    response = staff_client.get(_list_url())

    assert response.status_code == 200
    assert b"No banners yet" in response.content


@pytest.mark.django_db
def test_banner_list_shows_existing_banners(staff_client):
    today = datetime.date.today()
    Banner.objects.create(
        image=_make_uploaded_image(),
        link_type=Banner.LinkType.NONE,
        start_date=today,
        end_date=today + datetime.timedelta(days=5),
    )

    response = staff_client.get(_list_url())

    content = response.content.decode()
    assert "Active" in content


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_staff_can_create_a_banner_with_no_link(staff_client):
    payload = _valid_payload()
    payload["image"] = _make_uploaded_image()

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 302
    banner = Banner.objects.get()
    assert banner.link_type == Banner.LinkType.NONE
    assert banner.get_link_url() is None


@pytest.mark.django_db
def test_staff_can_create_a_banner_linking_to_a_product(staff_client, product):
    payload = _valid_payload(link_type=Banner.LinkType.PRODUCT, product=str(product.pk))
    payload["image"] = _make_uploaded_image()

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 302
    banner = Banner.objects.get()
    assert banner.product_id == product.pk
    assert banner.get_link_url() == reverse(
        "catalog:product_detail", args=[product.slug]
    )


@pytest.mark.django_db
def test_staff_can_create_a_banner_linking_to_a_category(staff_client, category):
    payload = _valid_payload(
        link_type=Banner.LinkType.CATEGORY, category=str(category.pk)
    )
    payload["image"] = _make_uploaded_image()

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 302
    banner = Banner.objects.get()
    assert banner.category_id == category.pk


@pytest.mark.django_db
def test_staff_can_create_a_banner_linking_to_a_custom_page(staff_client):
    payload = _valid_payload(
        link_type=Banner.LinkType.PAGE, url="https://bancostore.com/about/"
    )
    payload["image"] = _make_uploaded_image()

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 302
    banner = Banner.objects.get()
    assert banner.url == "https://bancostore.com/about/"


@pytest.mark.django_db
def test_creating_a_product_banner_without_choosing_a_product_is_rejected(
    staff_client,
):
    payload = _valid_payload(link_type=Banner.LinkType.PRODUCT)
    payload["image"] = _make_uploaded_image()

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 200
    assert not Banner.objects.exists()


@pytest.mark.django_db
def test_creating_a_page_banner_without_a_url_is_rejected(staff_client):
    payload = _valid_payload(link_type=Banner.LinkType.PAGE)
    payload["image"] = _make_uploaded_image()

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 200
    assert not Banner.objects.exists()


@pytest.mark.django_db
def test_creating_a_banner_with_end_date_before_start_date_is_rejected(staff_client):
    today = datetime.date.today()
    payload = _valid_payload(
        start_date=today.isoformat(),
        end_date=(today - datetime.timedelta(days=1)).isoformat(),
    )
    payload["image"] = _make_uploaded_image()

    response = staff_client.post(_create_url(), payload)

    assert response.status_code == 200
    assert not Banner.objects.exists()


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_edit_form_pre_fills_the_existing_dates_through_the_themed_picker(
    staff_client,
):
    """The date fields switched from a native <input type=date> to the
    shared admin_portal/_date_filter_field.html themed picker (json_script
    -> Alpine, not a plain widget-rendered value attribute) -- this is new
    wiring in this file specifically, not covered by the POST-only edit
    test below, so a GET regression here would otherwise ship silently."""
    today = datetime.date.today()
    banner = Banner.objects.create(
        image=_make_uploaded_image(),
        start_date=today,
        end_date=today + datetime.timedelta(days=5),
    )

    response = staff_client.get(_edit_url(banner))

    content = response.content.decode()
    assert f'"{today.isoformat()}"' in content
    assert f'"{(today + datetime.timedelta(days=5)).isoformat()}"' in content


@pytest.mark.django_db
def test_staff_can_edit_a_banners_dates(staff_client):
    today = datetime.date.today()
    banner = Banner.objects.create(
        image=_make_uploaded_image(),
        start_date=today,
        end_date=today + datetime.timedelta(days=5),
    )

    new_end = today + datetime.timedelta(days=30)
    response = staff_client.post(
        _edit_url(banner), _valid_payload(end_date=new_end.isoformat())
    )

    assert response.status_code == 302
    banner.refresh_from_db()
    assert banner.end_date == new_end


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_staff_can_delete_a_banner(staff_client):
    today = datetime.date.today()
    banner = Banner.objects.create(
        image=_make_uploaded_image(),
        start_date=today,
        end_date=today + datetime.timedelta(days=5),
    )

    response = staff_client.post(_delete_url(banner))

    assert response.status_code == 302
    assert not Banner.objects.filter(pk=banner.pk).exists()
