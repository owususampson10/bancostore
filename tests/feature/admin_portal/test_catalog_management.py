import io
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

import pytest
from PIL import Image

from apps.admin_portal.views import LOW_STOCK_THRESHOLD
from apps.catalog.models import Category, Product, ProductImage, ProductVariant
from apps.orders.models import Order, OrderItem

User = get_user_model()


def _category_list_url():
    return reverse("admin_portal:catalog_category_list")


def _category_create_url():
    return reverse("admin_portal:catalog_category_create")


def _category_edit_url(category):
    return reverse("admin_portal:catalog_category_edit", args=[category.pk])


def _category_delete_url(category):
    return reverse("admin_portal:catalog_category_delete", args=[category.pk])


def _product_list_url():
    return reverse("admin_portal:catalog_product_list")


def _product_create_url():
    return reverse("admin_portal:catalog_product_create")


def _product_edit_url(product):
    return reverse("admin_portal:catalog_product_edit", args=[product.pk])


def _product_delete_url(product):
    return reverse("admin_portal:catalog_product_delete", args=[product.pk])


def _make_category(name="Watches"):
    return Category.objects.create(name=name)


def _make_uploaded_image(name="photo.jpg"):
    buffer = io.BytesIO()
    Image.new("RGB", (400, 300), "red").save(buffer, format="JPEG")
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type="image/jpeg")


# ---------------------------------------------------------------------------
# Category list
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_category_list_shows_existing_categories(staff_client):
    Category.objects.create(name="Fine Jewellery")

    response = staff_client.get(_category_list_url())

    assert response.status_code == 200
    assert b"Fine Jewellery" in response.content


@pytest.mark.django_db
def test_category_list_shows_product_count_per_category(staff_client):
    category = Category.objects.create(name="Watches")
    Product.objects.create(name="Watch A", category=category, price=Decimal("100.00"))
    Product.objects.create(name="Watch B", category=category, price=Decimal("200.00"))

    response = staff_client.get(_category_list_url())

    body = response.content.decode()
    assert ">2<" in body


@pytest.mark.django_db
def test_category_list_shows_an_empty_state_with_no_categories(staff_client):
    response = staff_client.get(_category_list_url())

    assert b"No categories yet" in response.content


@pytest.mark.django_db
def test_category_search_matches_by_name(staff_client):
    Category.objects.create(name="Fine Jewellery")
    Category.objects.create(name="Leather Goods")

    response = staff_client.get(_category_list_url(), {"q": "Jewellery"})

    body = response.content.decode()
    assert "Fine Jewellery" in body
    assert "Leather Goods" not in body


@pytest.mark.django_db
def test_htmx_category_search_returns_only_the_results_partial(staff_client):
    Category.objects.create(name="Fine Jewellery")

    response = staff_client.get(
        _category_list_url(), {"q": "Fine"}, headers={"HX-Request": "true"}
    )

    body = response.content.decode()
    assert "Fine Jewellery" in body
    assert "Bancostore Admin Portal" not in body


@pytest.mark.django_db
def test_a_non_staff_user_is_forbidden_from_the_category_list(client, db):
    user = User.objects.create_user(username="regular", password="Passw0rd!")
    client.force_login(user)

    response = client.get(_category_list_url())

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Category create/edit
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_creating_a_category_derives_its_slug(staff_client):
    response = staff_client.post(
        _category_create_url(), {"name": "Fine Jewellery"}, follow=True
    )

    assert response.status_code == 200
    category = Category.objects.get(name="Fine Jewellery")
    assert category.slug == "fine-jewellery"


@pytest.mark.django_db
def test_editing_a_category_updates_its_name(staff_client):
    category = Category.objects.create(name="Old Name")

    response = staff_client.post(
        _category_edit_url(category), {"name": "New Name"}, follow=True
    )

    assert response.status_code == 200
    category.refresh_from_db()
    assert category.name == "New Name"


@pytest.mark.django_db
def test_creating_a_category_with_a_duplicate_name_shows_a_form_error(staff_client):
    Category.objects.create(name="Fine Jewellery")

    response = staff_client.post(_category_create_url(), {"name": "Fine Jewellery"})

    assert response.status_code == 200
    assert Category.objects.filter(name="Fine Jewellery").count() == 1


# ---------------------------------------------------------------------------
# Category delete
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_deleting_an_empty_category_succeeds(staff_client):
    category = Category.objects.create(name="Unused Category")

    response = staff_client.post(_category_delete_url(category), follow=True)

    assert response.status_code == 200
    assert not Category.objects.filter(pk=category.pk).exists()


@pytest.mark.django_db
def test_deleting_a_category_with_products_shows_an_error_instead_of_500(
    staff_client,
):
    category = Category.objects.create(name="In Use")
    Product.objects.create(
        name="Still Selling", category=category, price=Decimal("50.00")
    )

    response = staff_client.post(_category_delete_url(category), follow=True)

    assert response.status_code == 200
    assert Category.objects.filter(pk=category.pk).exists()
    body = response.content.decode()
    assert "still has products" in body.lower() or "cannot be deleted" in body.lower()


# ---------------------------------------------------------------------------
# Product list + real-time filters
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_product_list_shows_existing_products(staff_client):
    category = _make_category()
    Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("100.00")
    )

    response = staff_client.get(_product_list_url())

    assert response.status_code == 200
    assert b"Classic Watch" in response.content


@pytest.mark.django_db
def test_product_list_shows_an_empty_state_with_no_products(staff_client):
    response = staff_client.get(_product_list_url())

    assert b"No products yet" in response.content


@pytest.mark.django_db
def test_product_search_matches_by_name(staff_client):
    category = _make_category()
    Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("100.00")
    )
    Product.objects.create(
        name="Gold Necklace", category=category, price=Decimal("200.00")
    )

    response = staff_client.get(_product_list_url(), {"q": "Watch"})

    body = response.content.decode()
    assert "Classic Watch" in body
    assert "Gold Necklace" not in body


@pytest.mark.django_db
def test_product_category_filter(staff_client):
    watches = _make_category(name="Watches")
    jewellery = _make_category(name="Jewellery")
    Product.objects.create(
        name="Classic Watch", category=watches, price=Decimal("100.00")
    )
    Product.objects.create(
        name="Gold Necklace", category=jewellery, price=Decimal("200.00")
    )

    response = staff_client.get(_product_list_url(), {"category": watches.pk})

    body = response.content.decode()
    assert "Classic Watch" in body
    assert "Gold Necklace" not in body


@pytest.mark.django_db
def test_product_status_filter_active(staff_client):
    category = _make_category()
    Product.objects.create(
        name="Active One", category=category, price=Decimal("10.00"), is_active=True
    )
    Product.objects.create(
        name="Inactive One", category=category, price=Decimal("10.00"), is_active=False
    )

    response = staff_client.get(_product_list_url(), {"status": "active"})

    body = response.content.decode()
    assert "Active One" in body
    assert "Inactive One" not in body


@pytest.mark.django_db
def test_product_featured_filter(staff_client):
    category = _make_category()
    Product.objects.create(
        name="Featured One", category=category, price=Decimal("10.00"), is_featured=True
    )
    Product.objects.create(
        name="Regular One", category=category, price=Decimal("10.00"), is_featured=False
    )

    response = staff_client.get(_product_list_url(), {"featured": "yes"})

    body = response.content.decode()
    assert "Featured One" in body
    assert "Regular One" not in body


@pytest.mark.django_db
def test_product_low_stock_filter_only_matches_products_under_the_threshold(
    staff_client,
):
    category = _make_category()
    Product.objects.create(
        name="Low Stock One",
        category=category,
        price=Decimal("10.00"),
        stock=5,
    )
    Product.objects.create(
        name="Well Stocked One",
        category=category,
        price=Decimal("10.00"),
        stock=50,
    )
    Product.objects.create(
        name="Exactly At Threshold",
        category=category,
        price=Decimal("10.00"),
        stock=LOW_STOCK_THRESHOLD,
    )

    response = staff_client.get(_product_list_url(), {"low_stock": "1"})

    body = response.content.decode()
    assert "Low Stock One" in body
    assert "Well Stocked One" not in body
    # Strictly less-than, matching the dashboard count's own semantics --
    # a product exactly at the threshold isn't "low" yet.
    assert "Exactly At Threshold" not in body


@pytest.mark.django_db
def test_htmx_product_search_returns_only_the_results_partial(staff_client):
    category = _make_category()
    Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("100.00")
    )

    response = staff_client.get(
        _product_list_url(), {"q": "Watch"}, headers={"HX-Request": "true"}
    )

    body = response.content.decode()
    assert "Classic Watch" in body
    assert "Bancostore Admin Portal" not in body


@pytest.mark.django_db
def test_product_list_has_no_filter_submit_button(staff_client):
    """Real-time auto-filter, explicitly no Apply/Filter button -- the
    filter form's only trigger is keyup/change on its own inputs."""
    response = staff_client.get(_product_list_url())

    body = response.content.decode()
    form_start = body.index('id="product-filter-form"')
    form_end = body.index("</form>", form_start)
    form_html = body[form_start:form_end]
    assert 'type="submit"' not in form_html


# ---------------------------------------------------------------------------
# Product create/edit/delete
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_creating_a_product_derives_its_slug(staff_client):
    category = _make_category()

    response = staff_client.post(
        _product_create_url(),
        {
            "name": "Classic Watch",
            "category": category.pk,
            "description": "",
            "price": "150.00",
            "pv_value": "10",
            "stock": "5",
            "is_active": "on",
            "images-TOTAL_FORMS": "0",
            "images-INITIAL_FORMS": "0",
            "images-MIN_NUM_FORMS": "0",
            "images-MAX_NUM_FORMS": "1000",
            "variants-TOTAL_FORMS": "0",
            "variants-INITIAL_FORMS": "0",
            "variants-MIN_NUM_FORMS": "0",
            "variants-MAX_NUM_FORMS": "1000",
        },
        follow=True,
    )

    assert response.status_code == 200
    product = Product.objects.get(name="Classic Watch")
    assert product.slug == "classic-watch"
    assert product.stock == 5


@pytest.mark.django_db
def test_creating_a_product_with_variants(staff_client):
    category = _make_category()

    response = staff_client.post(
        _product_create_url(),
        {
            "name": "Classic Watch",
            "category": category.pk,
            "description": "",
            "price": "150.00",
            "pv_value": "10",
            "stock": "5",
            "images-TOTAL_FORMS": "0",
            "images-INITIAL_FORMS": "0",
            "images-MIN_NUM_FORMS": "0",
            "images-MAX_NUM_FORMS": "1000",
            "variants-TOTAL_FORMS": "1",
            "variants-INITIAL_FORMS": "0",
            "variants-MIN_NUM_FORMS": "0",
            "variants-MAX_NUM_FORMS": "1000",
            "variants-0-name": "Colour",
            "variants-0-value": "Gold",
        },
        follow=True,
    )

    assert response.status_code == 200
    product = Product.objects.get(name="Classic Watch")
    variant = ProductVariant.objects.get(product=product)
    assert variant.name == "Colour"
    assert variant.value == "Gold"


@pytest.mark.django_db
def test_editing_a_product_updates_its_price(staff_client):
    category = _make_category()
    product = Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("100.00")
    )

    response = staff_client.post(
        _product_edit_url(product),
        {
            "name": "Classic Watch",
            "category": category.pk,
            "description": "",
            "price": "175.00",
            "pv_value": "10",
            "stock": "5",
            "images-TOTAL_FORMS": "0",
            "images-INITIAL_FORMS": "0",
            "images-MIN_NUM_FORMS": "0",
            "images-MAX_NUM_FORMS": "1000",
            "variants-TOTAL_FORMS": "0",
            "variants-INITIAL_FORMS": "0",
            "variants-MIN_NUM_FORMS": "0",
            "variants-MAX_NUM_FORMS": "1000",
        },
        follow=True,
    )

    assert response.status_code == 200
    product.refresh_from_db()
    assert product.price == Decimal("175.00")


@pytest.mark.django_db
def test_deleting_a_product_succeeds(staff_client):
    category = _make_category()
    product = Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("100.00")
    )

    response = staff_client.post(_product_delete_url(product), follow=True)

    assert response.status_code == 200
    assert not Product.objects.filter(pk=product.pk).exists()


@pytest.mark.django_db
def test_a_non_staff_user_is_forbidden_from_the_product_list(client, db):
    user = User.objects.create_user(username="regular2", password="Passw0rd!")
    client.force_login(user)

    response = client.get(_product_list_url())

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Product add/edit page rendering + image uploads
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_add_product_page_renders(staff_client):
    _make_category()

    response = staff_client.get(_product_create_url())

    assert response.status_code == 200
    assert b"Add New Product" in response.content


@pytest.mark.django_db
def test_edit_product_page_renders_with_existing_values(staff_client):
    category = _make_category()
    product = Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("100.00")
    )

    response = staff_client.get(_product_edit_url(product))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Classic Watch" in body
    assert "Edit Product" in body


@pytest.mark.django_db
def test_creating_a_product_with_an_uploaded_image(staff_client):
    category = _make_category()

    response = staff_client.post(
        _product_create_url(),
        {
            "name": "Classic Watch",
            "category": category.pk,
            "description": "",
            "price": "150.00",
            "pv_value": "10",
            "stock": "5",
            "images-TOTAL_FORMS": "1",
            "images-INITIAL_FORMS": "0",
            "images-MIN_NUM_FORMS": "0",
            "images-MAX_NUM_FORMS": "1000",
            "images-0-image": _make_uploaded_image(),
            "images-0-is_primary": "on",
            "images-0-order": "0",
            "variants-TOTAL_FORMS": "0",
            "variants-INITIAL_FORMS": "0",
            "variants-MIN_NUM_FORMS": "0",
            "variants-MAX_NUM_FORMS": "1000",
        },
        follow=True,
    )

    assert response.status_code == 200
    product = Product.objects.get(name="Classic Watch")
    image = ProductImage.objects.get(product=product)
    assert image.is_primary is True


@pytest.mark.django_db
def test_marking_two_images_primary_leaves_only_one_primary_after_save(staff_client):
    """End-to-end proof that normalize_primary_image is actually wired
    into the create/edit view, not just unit-tested in isolation."""
    category = _make_category()

    response = staff_client.post(
        _product_create_url(),
        {
            "name": "Classic Watch",
            "category": category.pk,
            "description": "",
            "price": "150.00",
            "pv_value": "10",
            "stock": "5",
            "images-TOTAL_FORMS": "2",
            "images-INITIAL_FORMS": "0",
            "images-MIN_NUM_FORMS": "0",
            "images-MAX_NUM_FORMS": "1000",
            "images-0-image": _make_uploaded_image("a.jpg"),
            "images-0-is_primary": "on",
            "images-0-order": "0",
            "images-1-image": _make_uploaded_image("b.jpg"),
            "images-1-is_primary": "on",
            "images-1-order": "1",
            "variants-TOTAL_FORMS": "0",
            "variants-INITIAL_FORMS": "0",
            "variants-MIN_NUM_FORMS": "0",
            "variants-MAX_NUM_FORMS": "1000",
        },
        follow=True,
    )

    assert response.status_code == 200
    product = Product.objects.get(name="Classic Watch")
    primary_count = ProductImage.objects.filter(
        product=product, is_primary=True
    ).count()
    assert primary_count == 1


# ---------------------------------------------------------------------------
# Image cap (max 5 total)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_add_product_page_renders_exactly_five_image_upload_slots(staff_client):
    _make_category()

    response = staff_client.get(_product_create_url())

    body = response.content.decode()
    assert (
        'name="id_images-TOTAL_FORMS"' not in body
    )  # sanity: real id is id_images-TOTAL_FORMS
    assert '<input type="hidden" name="images-TOTAL_FORMS" value="5"' in body


@pytest.mark.django_db
def test_edit_product_page_only_renders_remaining_slots_up_to_five(staff_client):
    category = _make_category()
    product = Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("100.00")
    )
    ProductImage.objects.create(
        product=product, image=_make_uploaded_image("a.jpg"), order=0
    )
    ProductImage.objects.create(
        product=product, image=_make_uploaded_image("b.jpg"), order=1
    )

    response = staff_client.get(_product_edit_url(product))

    body = response.content.decode()
    # 2 existing + 3 blank slots = 5 total, never more.
    assert '<input type="hidden" name="images-TOTAL_FORMS" value="5"' in body


@pytest.mark.django_db
def test_edit_product_page_renders_zero_extra_slots_once_five_images_exist(
    staff_client,
):
    category = _make_category()
    product = Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("100.00")
    )
    for i in range(5):
        ProductImage.objects.create(
            product=product, image=_make_uploaded_image(f"{i}.jpg"), order=i
        )

    response = staff_client.get(_product_edit_url(product))

    body = response.content.decode()
    assert '<input type="hidden" name="images-TOTAL_FORMS" value="5"' in body


@pytest.mark.django_db
def test_submitting_six_images_is_rejected_and_creates_no_product(staff_client):
    category = _make_category()

    data = {
        "name": "Classic Watch",
        "category": category.pk,
        "description": "",
        "price": "150.00",
        "pv_value": "10",
        "stock": "5",
        "images-TOTAL_FORMS": "6",
        "images-INITIAL_FORMS": "0",
        "images-MIN_NUM_FORMS": "0",
        "images-MAX_NUM_FORMS": "1000",
        "variants-TOTAL_FORMS": "0",
        "variants-INITIAL_FORMS": "0",
        "variants-MIN_NUM_FORMS": "0",
        "variants-MAX_NUM_FORMS": "1000",
    }
    for i in range(6):
        data[f"images-{i}-image"] = _make_uploaded_image(f"{i}.jpg")
        data[f"images-{i}-order"] = str(i)

    response = staff_client.post(_product_create_url(), data)

    assert response.status_code == 200
    assert not Product.objects.filter(name="Classic Watch").exists()


# ---------------------------------------------------------------------------
# CodeRabbit findings on PR #53
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_product_list_ignores_a_non_numeric_category_filter_instead_of_500(
    staff_client,
):
    """A crafted/stale ?category=abc must not 500 -- category_id is only
    ever a real pk in the rendered listbox, but the querystring is fully
    attacker-controlled."""
    response = staff_client.get(_product_list_url(), {"category": "not-a-number"})

    assert response.status_code == 200


@pytest.mark.django_db
def test_deleting_a_product_that_has_been_ordered_shows_an_error_instead_of_500(
    staff_client,
):
    """OrderItem.product is on_delete=PROTECT (apps/orders/models.py) --
    order history must never be destroyed by deleting the product it
    references, mirroring the same protection already proven for
    Category delete."""
    category = _make_category()
    product = Product.objects.create(
        name="Once Ordered", category=category, price=Decimal("100.00")
    )
    order = Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        delivery_method=Order.DeliveryMethod.HOME_DELIVERY,
        delivery_zone=Order.DeliveryZone.ACCRA,
        address="12 High St",
        area="Osu",
        subtotal=Decimal("100.00"),
        delivery_fee=Decimal("50.00"),
        total=Decimal("150.00"),
        pv_earned=0,
        payment_reference="protected-delete-test",
        status=Order.Status.CONFIRMED,
    )
    OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        quantity=1,
        unit_price=product.price,
        unit_pv=0,
    )

    response = staff_client.post(_product_delete_url(product), follow=True)

    assert response.status_code == 200
    assert Product.objects.filter(pk=product.pk).exists()


@pytest.mark.django_db
def test_product_thumbnail_alt_text_uses_the_product_name(staff_client):
    category = _make_category()
    product = Product.objects.create(
        name="Classic Watch", category=category, price=Decimal("100.00")
    )
    ProductImage.objects.create(
        product=product, image=_make_uploaded_image(), is_primary=True
    )

    response = staff_client.get(_product_list_url())

    assert b'alt="Classic Watch"' in response.content


@pytest.mark.django_db
def test_submitting_too_many_images_shows_the_formset_max_error(staff_client):
    """validate_max=True on the image formset (Task 26) silently rejected
    an over-cap submission with no visible explanation -- an admin who
    hits it just saw their form "not save" with no reason why."""
    category = _make_category()

    data = {
        "name": "Classic Watch",
        "category": category.pk,
        "description": "",
        "price": "150.00",
        "pv_value": "10",
        "stock": "5",
        "images-TOTAL_FORMS": "6",
        "images-INITIAL_FORMS": "0",
        "images-MIN_NUM_FORMS": "0",
        "images-MAX_NUM_FORMS": "1000",
        "variants-TOTAL_FORMS": "0",
        "variants-INITIAL_FORMS": "0",
        "variants-MIN_NUM_FORMS": "0",
        "variants-MAX_NUM_FORMS": "1000",
    }
    for i in range(6):
        data[f"images-{i}-image"] = _make_uploaded_image(f"{i}.jpg")
        data[f"images-{i}-order"] = str(i)

    response = staff_client.post(_product_create_url(), data)

    assert response.status_code == 200
    assert b"Please submit at most 5 forms." in response.content
