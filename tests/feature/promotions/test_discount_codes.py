import datetime
from decimal import Decimal

import pytest

from apps.promotions.models import DiscountCode


def _make_code(**overrides):
    today = datetime.date.today()
    defaults = {
        "code": "SAVE20",
        "discount_type": DiscountCode.DiscountType.FIXED,
        "amount": Decimal("20.00"),
        "expiry_date": today + datetime.timedelta(days=30),
        "audience": DiscountCode.Audience.EVERYONE,
        "max_uses": 100,
    }
    defaults.update(overrides)
    return DiscountCode.objects.create(**defaults)


@pytest.mark.django_db
def test_code_is_normalized_to_uppercase_on_save():
    """Matches this project's own real-world-convention reasoning
    (Shopify/WooCommerce/Stripe all treat discount codes as
    case-insensitive) -- storing a normalized uppercase value means
    lookup at checkout never has to guess at casing."""
    code = _make_code(code="save20")

    assert code.code == "SAVE20"


@pytest.mark.django_db
def test_calculate_discount_for_fixed_amount_type():
    code = _make_code(
        discount_type=DiscountCode.DiscountType.FIXED, amount=Decimal("20.00")
    )

    assert code.calculate_discount(Decimal("100.00")) == Decimal("20.00")


@pytest.mark.django_db
def test_calculate_discount_for_percentage_type():
    code = _make_code(
        discount_type=DiscountCode.DiscountType.PERCENTAGE, amount=Decimal("10.00")
    )

    assert code.calculate_discount(Decimal("200.00")) == Decimal("20.00")


@pytest.mark.django_db
def test_fixed_discount_never_exceeds_the_order_subtotal():
    """A GHS 50 off code against a GHS 30 order must never produce a
    negative total -- capped at the subtotal itself, not the code's own
    face value."""
    code = _make_code(
        discount_type=DiscountCode.DiscountType.FIXED, amount=Decimal("50.00")
    )

    assert code.calculate_discount(Decimal("30.00")) == Decimal("30.00")


@pytest.mark.django_db
def test_percentage_discount_rounds_to_the_pesewa():
    """Matches this codebase's own established money-rounding convention
    (Decimal.quantize(..., ROUND_HALF_UP) to the pesewa, apps/commissions)
    rather than leaving a fractional-pesewa amount."""
    code = _make_code(
        discount_type=DiscountCode.DiscountType.PERCENTAGE, amount=Decimal("33.33")
    )

    result = code.calculate_discount(Decimal("10.00"))

    assert result == Decimal("3.33")
    assert result.as_tuple().exponent == -2


@pytest.mark.django_db
def test_is_redeemable_is_true_for_an_active_unexpired_unexhausted_code():
    code = _make_code()

    assert code.is_redeemable() is True


@pytest.mark.django_db
def test_is_redeemable_is_false_when_inactive():
    code = _make_code(is_active=False)

    assert code.is_redeemable() is False


@pytest.mark.django_db
def test_is_redeemable_is_false_when_expired():
    today = datetime.date.today()
    code = _make_code(expiry_date=today - datetime.timedelta(days=1))

    assert code.is_redeemable() is False


@pytest.mark.django_db
def test_is_redeemable_is_true_on_its_exact_expiry_date():
    today = datetime.date.today()
    code = _make_code(expiry_date=today)

    assert code.is_redeemable() is True


@pytest.mark.django_db
def test_is_redeemable_is_false_when_usage_cap_reached():
    code = _make_code(max_uses=5, times_used=5)

    assert code.is_redeemable() is False


@pytest.mark.django_db
def test_is_redeemable_is_true_just_under_the_usage_cap():
    code = _make_code(max_uses=5, times_used=4)

    assert code.is_redeemable() is True
