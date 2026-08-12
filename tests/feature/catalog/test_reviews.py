from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse

import pytest

from apps.catalog.models import Category, Product, Review
from apps.orders.models import Order, OrderItem

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


def _make_delivered_order(user, product, quantity=1):
    order = Order.objects.create(
        customer=user,
        full_name="Test Customer",
        phone_number="+233241234567",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=product.price * quantity,
        delivery_fee=Decimal("0"),
        total=product.price * quantity,
        payment_reference=f"ref-{user.pk}-{product.pk}",
        status=Order.Status.DELIVERED,
    )
    OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        quantity=quantity,
        unit_price=product.price,
        unit_pv=0,
    )
    return order


@pytest.mark.django_db
def test_product_detail_shows_review_control_for_a_customer_with_a_delivered_order(
    client, product
):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    _make_delivered_order(user, product)
    client.force_login(user)

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    assert response.context["can_review"] is True


@pytest.mark.django_db
def test_product_detail_hides_review_control_with_no_delivered_order(client, product):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    assert response.context["can_review"] is False


@pytest.mark.django_db
def test_product_detail_hides_review_control_for_a_guest(client, product):
    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    assert response.context["can_review"] is False


@pytest.mark.django_db
def test_a_pending_order_does_not_grant_review_eligibility(client, product):
    """Only delivered orders count -- a still-in-transit purchase hasn't
    actually let the customer use the product yet."""
    user = User.objects.create_user(username="ama@example.test", password="pw")
    order = _make_delivered_order(user, product)
    order.status = Order.Status.PROCESSING
    order.save()
    client.force_login(user)

    response = client.get(reverse("catalog:product_detail", args=[product.slug]))

    assert response.context["can_review"] is False


@pytest.mark.django_db
def test_review_submission_requires_login(client, product):
    response = client.post(
        reverse("catalog:review_submit", args=[product.pk]),
        {"rating": 5, "body": "Great product"},
    )

    assert response.status_code == 302
    assert reverse("account_login") in response["Location"]
    assert not Review.objects.exists()


@pytest.mark.django_db
def test_ineligible_customer_cannot_submit_a_review(client, product):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    client.force_login(user)

    response = client.post(
        reverse("catalog:review_submit", args=[product.pk]),
        {"rating": 5, "body": "Great product"},
    )

    assert response.status_code == 403
    assert not Review.objects.exists()


@pytest.mark.django_db
def test_eligible_customer_can_submit_a_review(client, product):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    _make_delivered_order(user, product)
    client.force_login(user)

    response = client.post(
        reverse("catalog:review_submit", args=[product.pk]),
        {"rating": 4, "body": "Really liked this."},
    )

    assert response.status_code == 302
    review = Review.objects.get(user=user, product=product)
    assert review.rating == 4
    assert review.body == "Really liked this."
    assert review.is_approved is False


@pytest.mark.django_db
def test_resubmitting_a_review_edits_the_existing_one_in_place(client, product):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    _make_delivered_order(user, product)
    client.force_login(user)

    client.post(
        reverse("catalog:review_submit", args=[product.pk]),
        {"rating": 3, "body": "It was okay."},
    )
    client.post(
        reverse("catalog:review_submit", args=[product.pk]),
        {"rating": 5, "body": "Actually, it grew on me."},
    )

    assert Review.objects.filter(user=user, product=product).count() == 1
    review = Review.objects.get(user=user, product=product)
    assert review.rating == 5
    assert review.body == "Actually, it grew on me."


@pytest.mark.django_db
def test_resubmitting_an_approved_review_resets_it_to_unapproved(client, product):
    """A materially different review shouldn't stay silently approved with
    new, never-moderated content."""
    user = User.objects.create_user(username="ama@example.test", password="pw")
    _make_delivered_order(user, product)
    client.force_login(user)
    review = Review.objects.create(
        user=user, product=product, rating=5, body="Original", is_approved=True
    )

    client.post(
        reverse("catalog:review_submit", args=[product.pk]),
        {"rating": 1, "body": "Changed my mind entirely."},
    )

    review.refresh_from_db()
    assert review.is_approved is False


@pytest.mark.django_db
def test_review_requires_a_rating_and_body(client, product):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    _make_delivered_order(user, product)
    client.force_login(user)

    response = client.post(
        reverse("catalog:review_submit", args=[product.pk]), {"rating": "", "body": ""}
    )

    assert response.status_code == 200
    assert not Review.objects.exists()


@pytest.mark.django_db
def test_rating_must_be_between_one_and_five(client, product):
    user = User.objects.create_user(username="ama@example.test", password="pw")
    _make_delivered_order(user, product)
    client.force_login(user)

    response = client.post(
        reverse("catalog:review_submit", args=[product.pk]),
        {"rating": 6, "body": "Too enthusiastic"},
    )

    assert response.status_code == 200
    assert not Review.objects.exists()


@pytest.mark.django_db
def test_a_race_between_two_first_time_submissions_updates_instead_of_erroring(
    client, product
):
    """code-review-and-quality finding, fixed pre-merge: the existing-
    review lookup in review_submit can return None while a concurrent
    request has already inserted the row by the time this request's own
    save() runs -- the model's UniqueConstraint(user, product) turns that
    into an IntegrityError, which must be caught and turned into an
    update, not surfaced as a 500. Simulates the race deterministically
    (mocking the lookup to miss a row that's already really there) rather
    than a real multi-threaded test, matching this feature's own scope --
    SPEC_PHASE2.md reserves that heavier concurrency-test investment for
    Discount Codes/Escrow, not this low-stakes UX feature."""
    user = User.objects.create_user(username="ama@example.test", password="pw")
    _make_delivered_order(user, product)
    client.force_login(user)
    # The "concurrent winner" -- a real row already exists by the time
    # this request's own save() runs.
    Review.objects.create(
        user=user, product=product, rating=1, body="First writer wins"
    )

    # Narrowed per CodeRabbit (PR #73): patching Review.objects.filter
    # unconditionally would also affect _product_detail_context's own
    # filter() call if the invalid-form branch were ever exercised under
    # the same patch -- only the first lookup in review_submit needs to
    # miss the already-real row.
    #
    # code-review-and-quality finding, fixed pre-merge: an earlier version
    # of this narrowing used `with patch.object(type(queryset), "first",
    # ...): return queryset` -- but a `return` inside a `with` block runs
    # __exit__ (un-patching) *before* control reaches the caller, so by the
    # time review_submit actually called .first(), the patch was already
    # reverted and the real row was returned. That made this test pass for
    # the wrong reason -- the IntegrityError fallback branch it exists to
    # prove was never actually exercised. Fixed by assigning directly onto
    # the fresh per-call queryset instance instead (no context manager,
    # nothing to un-patch): Python allows shadowing a bound method on one
    # instance without touching the class.
    real_filter = Review.objects.filter

    def _miss_once(*args, **kwargs):
        queryset = real_filter(*args, **kwargs)
        queryset.first = lambda: None
        return queryset

    # A spy on Review.objects.get, not just the final row state: the normal
    # (non-race) path and the IntegrityError fallback path both end up with
    # one row holding the second writer's content, so asserting only on
    # final state can't tell them apart -- exactly how the earlier broken
    # mock's version of this test kept passing for the wrong reason.
    # review_submit's except-branch is the *only* code path that calls
    # Review.objects.get(), so a call recorded here is direct proof the
    # race-handling branch actually ran, not just that the end state looks
    # the same as if it hadn't.
    with (
        patch("apps.catalog.views.Review.objects.filter", side_effect=_miss_once),
        patch(
            "apps.catalog.views.Review.objects.get", wraps=Review.objects.get
        ) as mock_get,
    ):
        response = client.post(
            reverse("catalog:review_submit", args=[product.pk]),
            {"rating": 5, "body": "Second writer's real submission"},
        )

    assert mock_get.called, (
        "Review.objects.get() was never called -- the IntegrityError fallback "
        "branch didn't run, meaning the mocked lookup miss wasn't real"
    )

    assert response.status_code == 302
    assert Review.objects.filter(user=user, product=product).count() == 1
    review = Review.objects.get(user=user, product=product)
    assert review.rating == 5
    assert review.body == "Second writer's real submission"
