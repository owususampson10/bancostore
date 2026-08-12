from decimal import Decimal

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product, Review

User = get_user_model()


def _reviews_url():
    return reverse("admin_portal:review_moderation_list")


def _approve_url(review):
    return reverse("admin_portal:review_approve", args=[review.pk])


def _delete_url(review):
    return reverse("admin_portal:review_delete", args=[review.pk])


@pytest.fixture
def product():
    category = Category.objects.create(name="Wellness", slug="wellness")
    return Product.objects.create(
        name="Vitality Pulse Smart Ring", category=category, price=Decimal("450.00")
    )


@pytest.mark.django_db
def test_a_non_staff_user_is_forbidden_from_the_review_queue(client, product):
    user = User.objects.create_user(username="regular", password="Passw0rd!")
    Review.objects.create(user=user, product=product, rating=5, body="Great!")
    client.force_login(user)

    response = client.get(_reviews_url())

    assert response.status_code == 403


@pytest.mark.django_db
def test_review_queue_lists_pending_and_approved_reviews(staff_client, product):
    author = User.objects.create_user(username="ama@example.test", password="pw")
    Review.objects.create(
        user=author, product=product, rating=5, body="Pending review text"
    )

    response = staff_client.get(_reviews_url())

    assert "Pending review text" in response.content.decode()


@pytest.mark.django_db
def test_staff_can_approve_a_review(staff_client, product):
    author = User.objects.create_user(username="ama@example.test", password="pw")
    review = Review.objects.create(
        user=author, product=product, rating=5, body="Great!", is_approved=False
    )

    response = staff_client.post(_approve_url(review))

    assert response.status_code == 302
    review.refresh_from_db()
    assert review.is_approved is True


@pytest.mark.django_db
def test_staff_can_delete_a_review(staff_client, product):
    author = User.objects.create_user(username="ama@example.test", password="pw")
    review = Review.objects.create(
        user=author, product=product, rating=1, body="Not great"
    )

    response = staff_client.post(_delete_url(review))

    assert response.status_code == 302
    assert not Review.objects.filter(pk=review.pk).exists()


@pytest.mark.django_db
def test_approve_and_delete_are_post_only(staff_client, product):
    author = User.objects.create_user(username="ama@example.test", password="pw")
    review = Review.objects.create(user=author, product=product, rating=5, body="Hi")

    approve_response = staff_client.get(_approve_url(review))
    delete_response = staff_client.get(_delete_url(review))

    assert approve_response.status_code == 405
    assert delete_response.status_code == 405


@pytest.mark.django_db
def test_product_detail_shows_only_approved_reviews(client, product):
    author = User.objects.create_user(username="ama@example.test", password="pw")
    Review.objects.create(
        user=author,
        product=product,
        rating=5,
        body="Approved text here",
        is_approved=True,
    )
    Review.objects.create(
        user=User.objects.create_user(username="kofi@example.test", password="pw"),
        product=product,
        rating=1,
        body="Unapproved text here",
        is_approved=False,
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    content = response.content.decode()
    assert "Approved text here" in content
    assert "Unapproved text here" not in content


@pytest.mark.django_db
def test_product_detail_shows_the_average_rating_of_approved_reviews_only(
    client, product
):
    User.objects.create_user(username="a@example.test", password="pw")
    Review.objects.create(
        user=User.objects.get(username="a@example.test"),
        product=product,
        rating=5,
        body="Five stars",
        is_approved=True,
    )
    Review.objects.create(
        user=User.objects.create_user(username="b@example.test", password="pw"),
        product=product,
        rating=1,
        body="One star",
        is_approved=False,
    )

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    assert response.context["average_rating"] == 5


@pytest.mark.django_db
def test_product_detail_average_rating_is_none_with_no_approved_reviews(
    client, product
):
    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    assert response.context["average_rating"] is None


@pytest.mark.django_db
def test_auto_approve_setting_makes_a_new_review_immediately_visible(
    client, product, settings
):
    from constance import config

    config.PRODUCT_REVIEW_AUTO_APPROVE_ENABLED = True
    user = User.objects.create_user(username="ama@example.test", password="pw")
    from apps.orders.models import Order, OrderItem

    order = Order.objects.create(
        customer=user,
        full_name="Test Customer",
        phone_number="+233241234567",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=product.price,
        delivery_fee=Decimal("0"),
        total=product.price,
        payment_reference="ref-auto-approve",
        status=Order.Status.DELIVERED,
    )
    OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        quantity=1,
        unit_price=product.price,
        unit_pv=0,
    )
    client.force_login(user)

    client.post(
        reverse("catalog:review_submit", args=[product.pk]),
        {"rating": 5, "body": "Auto approved!"},
    )

    review = Review.objects.get(user=user, product=product)
    assert review.is_approved is True
