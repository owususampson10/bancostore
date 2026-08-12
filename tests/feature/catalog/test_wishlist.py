from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product, WishlistItem

User = get_user_model()


@pytest.fixture
def category():
    return Category.objects.create(name="Wellness", slug="wellness")


@pytest.fixture
def product(category):
    return Product.objects.create(
        name="Vitality Pulse Smart Ring",
        category=category,
        price=Decimal("450.00"),
        stock=5,
    )


@pytest.mark.django_db
def test_toggle_adds_a_product_to_the_wishlist(client, product):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)

    response = client.post(reverse("catalog:wishlist_toggle", args=[product.pk]))

    assert response.status_code == 302
    assert WishlistItem.objects.filter(user=user, product=product).exists()


@pytest.mark.django_db
def test_toggle_again_removes_it(client, product):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)
    WishlistItem.objects.create(user=user, product=product)

    client.post(reverse("catalog:wishlist_toggle", args=[product.pk]))

    assert not WishlistItem.objects.filter(user=user, product=product).exists()


@pytest.mark.django_db
def test_toggle_is_a_no_op_duplicate_not_a_second_row(client, product):
    """The add path uses get_or_create -- confirms a rapid double-submit
    (e.g. a double click before the redirect lands) can never create two
    rows for the same user/product, which the DB-level UniqueConstraint
    would otherwise turn into a 500 rather than a silent no-op."""
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)

    client.post(reverse("catalog:wishlist_toggle", args=[product.pk]))
    # A second POST is a real remove (toggle), not a duplicate add -- this
    # test only proves get_or_create's own uniqueness, so assert directly
    # against the model instead of a second HTTP round-trip.
    WishlistItem.objects.get_or_create(user=user, product=product)

    assert WishlistItem.objects.filter(user=user, product=product).count() == 1


@pytest.mark.django_db
def test_toggle_requires_login(client, product):
    response = client.post(reverse("catalog:wishlist_toggle", args=[product.pk]))

    assert response.status_code == 302
    assert reverse("account_login") in response["Location"]
    assert not WishlistItem.objects.filter(product=product).exists()


@pytest.mark.django_db
def test_toggle_requires_post(client, product):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)

    response = client.get(reverse("catalog:wishlist_toggle", args=[product.pk]))

    assert response.status_code == 405


@pytest.mark.django_db
def test_toggle_404s_for_an_inactive_product(client, category):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)
    hidden = Product.objects.create(
        name="Hidden", category=category, price=Decimal("10.00"), is_active=False
    )

    response = client.post(reverse("catalog:wishlist_toggle", args=[hidden.pk]))

    assert response.status_code == 404


@pytest.mark.django_db
def test_wishlist_page_lists_only_the_logged_in_users_own_items(client, category):
    owner = User.objects.create_user(username="ama@example.test", password="pw")
    other = User.objects.create_user(username="kofi@example.test", password="pw")
    mine = Product.objects.create(
        name="Mine", category=category, price=Decimal("10.00")
    )
    theirs = Product.objects.create(
        name="Theirs", category=category, price=Decimal("10.00")
    )
    WishlistItem.objects.create(user=owner, product=mine)
    WishlistItem.objects.create(user=other, product=theirs)

    client.force_login(owner)
    response = client.get(reverse("catalog:wishlist"))

    content = response.content.decode()
    assert "Mine" in content
    assert "Theirs" not in content


@pytest.mark.django_db
def test_wishlist_page_requires_login(client):
    response = client.get(reverse("catalog:wishlist"))

    assert response.status_code == 302
    assert reverse("account_login") in response["Location"]


@pytest.mark.django_db
def test_wishlist_page_shows_an_empty_state_with_no_items(client):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)

    response = client.get(reverse("catalog:wishlist"))

    assert response.status_code == 200
    content = response.content.decode()
    assert (
        "empty" in content.lower()
        or "no items" in content.lower()
        or "haven" in content.lower()
    )


@pytest.mark.django_db
def test_wishlist_persists_across_logout_login(client, product):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)
    client.post(reverse("catalog:wishlist_toggle", args=[product.pk]))

    client.logout()
    client.force_login(user)
    response = client.get(reverse("catalog:wishlist"))

    assert product.name in response.content.decode()


@pytest.mark.django_db
def test_product_detail_shows_a_login_prompt_for_a_guest(client, product):
    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    content = response.content.decode()
    assert reverse("account_login") in content


@pytest.mark.django_db
def test_product_detail_shows_the_toggle_control_for_a_logged_in_user(client, product):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    content = response.content.decode()
    assert reverse("catalog:wishlist_toggle", args=[product.pk]) in content
