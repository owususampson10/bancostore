from decimal import Decimal

from django.contrib.sessions.middleware import SessionMiddleware
from django.test import RequestFactory

import pytest

from apps.catalog.models import Category, Product
from apps.orders.cart import Cart


def _request_with_session():
    request = RequestFactory().get("/")
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    return request


def _make_product(**overrides):
    category, _ = Category.objects.get_or_create(name="Wellness", slug="wellness")
    defaults = {
        "name": "Vitality Pulse Smart Ring",
        "category": category,
        "price": Decimal("450.00"),
        "pv_value": 60,
        "stock": 5,
        "is_active": True,
    }
    defaults.update(overrides)
    return Product.objects.create(**defaults)


@pytest.mark.django_db
def test_a_fresh_cart_is_empty():
    cart = Cart(_request_with_session())
    assert list(cart.items()) == []
    assert cart.count() == 0
    assert cart.subtotal() == Decimal("0")


@pytest.mark.django_db
def test_add_adds_a_product_with_the_given_quantity():
    product = _make_product(stock=10)
    cart = Cart(_request_with_session())

    cart.add(product, quantity=2)

    lines = cart.items()
    assert len(lines) == 1
    assert lines[0].product == product
    assert lines[0].quantity == 2
    assert lines[0].line_total == Decimal("900.00")


@pytest.mark.django_db
def test_add_twice_increments_the_existing_quantity():
    product = _make_product(stock=10)
    cart = Cart(_request_with_session())

    cart.add(product, quantity=1)
    cart.add(product, quantity=2)

    assert cart.items()[0].quantity == 3


@pytest.mark.django_db
def test_add_caps_quantity_at_current_stock():
    product = _make_product(stock=3)
    cart = Cart(_request_with_session())

    cart.add(product, quantity=10)

    assert cart.items()[0].quantity == 3


@pytest.mark.django_db
def test_add_returns_true_on_a_real_add():
    product = _make_product(stock=10)
    cart = Cart(_request_with_session())

    assert cart.add(product, quantity=1) is True


@pytest.mark.django_db
def test_add_returns_false_when_stock_is_exhausted():
    # code-review-and-quality (2026-07-24): the caller (cart_add view)
    # needs to distinguish "nothing happened" from "added" so it doesn't
    # redirect to /cart/ implying success when the product never entered
    # the cart at all.
    product = _make_product(stock=0)
    cart = Cart(_request_with_session())

    assert cart.add(product, quantity=1) is False
    assert cart.items() == []


@pytest.mark.django_db
def test_add_returns_false_when_already_at_the_stock_cap():
    product = _make_product(stock=2)
    cart = Cart(_request_with_session())
    cart.add(product, quantity=2)

    assert cart.add(product, quantity=1) is False


@pytest.mark.django_db
def test_update_sets_the_quantity_directly():
    product = _make_product(stock=10)
    cart = Cart(_request_with_session())
    cart.add(product, quantity=1)

    cart.update(product, quantity=5)

    assert cart.items()[0].quantity == 5


@pytest.mark.django_db
def test_update_caps_quantity_at_current_stock():
    product = _make_product(stock=3)
    cart = Cart(_request_with_session())
    cart.add(product, quantity=1)

    cart.update(product, quantity=10)

    assert cart.items()[0].quantity == 3


@pytest.mark.django_db
def test_update_to_zero_removes_the_item():
    product = _make_product(stock=10)
    cart = Cart(_request_with_session())
    cart.add(product, quantity=1)

    cart.update(product, quantity=0)

    assert cart.items() == []


@pytest.mark.django_db
def test_update_removes_the_item_when_stock_capping_leaves_zero():
    # CodeRabbit (PR #25): the original guard checked the pre-cap
    # quantity param against < 1, not the post-cap value -- a product
    # gone out of stock (stock=0) since being added would have stored a
    # zero-quantity line instead of removing it.
    product = _make_product(stock=10)
    cart = Cart(_request_with_session())
    cart.add(product, quantity=1)

    product.stock = 0
    product.save()
    cart.update(product, quantity=5)

    assert cart.items() == []


@pytest.mark.django_db
def test_remove_removes_the_item():
    product = _make_product(stock=10)
    cart = Cart(_request_with_session())
    cart.add(product, quantity=1)

    cart.remove(product)

    assert cart.items() == []


@pytest.mark.django_db
def test_remove_a_product_never_in_the_cart_is_a_no_op():
    product = _make_product(stock=10)
    cart = Cart(_request_with_session())

    cart.remove(product)

    assert cart.items() == []


@pytest.mark.django_db
def test_items_reflect_live_product_prices_not_a_snapshot():
    product = _make_product(price=Decimal("450.00"), stock=10)
    cart = Cart(_request_with_session())
    cart.add(product, quantity=1)

    product.price = Decimal("500.00")
    product.save()

    lines = Cart(_request_with_session_reusing(cart)).items()
    assert lines[0].line_total == Decimal("500.00")


def _request_with_session_reusing(cart):
    # Cart persists its state into request.session -- build a fresh Cart
    # bound to the SAME underlying session object to simulate "read it
    # back on a later request," the way the real session store would.
    request = RequestFactory().get("/")
    request.session = cart.session
    return request


@pytest.mark.django_db
def test_subtotal_sums_all_line_totals():
    product_a = _make_product(name="A", price=Decimal("100.00"), stock=10)
    product_b = _make_product(name="B", price=Decimal("50.00"), stock=10)
    cart = Cart(_request_with_session())

    cart.add(product_a, quantity=2)
    cart.add(product_b, quantity=3)

    assert cart.subtotal() == Decimal("350.00")


@pytest.mark.django_db
def test_count_sums_quantities_across_all_items():
    product_a = _make_product(name="A", stock=10)
    product_b = _make_product(name="B", stock=10)
    cart = Cart(_request_with_session())

    cart.add(product_a, quantity=2)
    cart.add(product_b, quantity=3)

    assert cart.count() == 5


@pytest.mark.django_db
def test_cart_survives_across_requests_sharing_the_same_session():
    product = _make_product(stock=10)
    first_request = _request_with_session()
    Cart(first_request).add(product, quantity=2)

    second_request = RequestFactory().get("/")
    second_request.session = first_request.session

    assert Cart(second_request).items()[0].quantity == 2


@pytest.mark.django_db
def test_a_different_session_starts_empty():
    product = _make_product(stock=10)
    Cart(_request_with_session()).add(product, quantity=2)

    assert Cart(_request_with_session()).items() == []


@pytest.mark.django_db
def test_a_deleted_product_is_pruned_from_the_session_when_items_is_read():
    # Real bug report: a distributor/customer adds an item, it later
    # vanishes from the cart with no explanation. Root cause -- a product
    # deleted after being added left a permanent stale entry: items()
    # correctly excluded it (the query just returns nothing for a
    # nonexistent pk) but count() kept including it forever, since it
    # only ever summed the raw session dict with no validation. The
    # header badge showed a phantom count while the cart page rendered
    # fully empty, with nothing to ever self-correct it.
    request = _request_with_session()
    product = _make_product(stock=10)
    cart = Cart(request)
    cart.add(product, quantity=2)
    assert cart.count() == 2

    product.delete()

    # Reading items() must prune the now-unresolvable entry from the
    # session, not just silently omit it from this one return value.
    assert Cart(request).items() == []
    assert Cart(request).count() == 0


@pytest.mark.django_db
def test_a_deactivated_product_is_pruned_the_same_way_as_a_deleted_one():
    # doubt-driven-development (Task 17c design review): a product an
    # admin deactivates -- not deletes -- after it's added to a cart is
    # just as unpurchasable as a deleted one, but the original fix only
    # excluded genuinely deleted pks. Left unfixed, checkout could
    # silently snapshot an OrderItem for a no-longer-purchasable product.
    request = _request_with_session()
    product = _make_product(stock=10, is_active=True)
    cart = Cart(request)
    cart.add(product, quantity=2)
    assert cart.count() == 2

    product.is_active = False
    product.save()

    assert Cart(request).items() == []
    assert Cart(request).count() == 0
