import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group

import pytest
from constance import config

from apps.orders.models import Order
from apps.promotions.models import DiscountCode
from apps.promotions.services import (
    InvalidDiscountCodeError,
    consume_discount_code,
    redeem_discount_code,
    release_discount_code,
)

User = get_user_model()


def _make_distributor_user():
    user = User.objects.create_user(username="+233551234567", password="pw")
    Group.objects.get_or_create(name="distributor")[0].user_set.add(user)
    return user


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


def _make_confirmed_order(**overrides):
    defaults = {
        "full_name": "Ama Mensah",
        "phone_number": "+233241234567",
        "delivery_method": Order.DeliveryMethod.PICKUP,
        "subtotal": Decimal("100.00"),
        "delivery_fee": Decimal("0.00"),
        "total": Decimal("100.00"),
        "payment_reference": f"order-discount-test-{Order.objects.count()}",
        "status": Order.Status.CONFIRMED,
    }
    defaults.update(overrides)
    return Order.objects.create(**defaults)


@pytest.fixture(autouse=True)
def _discount_codes_enabled():
    original = config.DISCOUNT_CODES_ENABLED
    original_cap = config.MAX_DISCOUNT_PER_ORDER
    config.DISCOUNT_CODES_ENABLED = True
    config.MAX_DISCOUNT_PER_ORDER = Decimal("500")
    try:
        yield
    finally:
        config.DISCOUNT_CODES_ENABLED = original
        config.MAX_DISCOUNT_PER_ORDER = original_cap


# ---------------------------------------------------------------------------
# redeem_discount_code
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_blank_code_returns_no_discount():
    code, amount = redeem_discount_code(
        "",
        subtotal=Decimal("100.00"),
        user=AnonymousUser(),
        phone_number="+233241234567",
    )

    assert code is None
    assert amount == Decimal("0")


@pytest.mark.django_db
def test_a_valid_code_is_case_insensitive_and_returns_the_discount_amount():
    _make_code(code="SAVE20", amount=Decimal("20.00"))

    code, amount = redeem_discount_code(
        "save20",
        subtotal=Decimal("100.00"),
        user=AnonymousUser(),
        phone_number="+233241234567",
    )

    assert code.code == "SAVE20"
    assert amount == Decimal("20.00")


@pytest.mark.django_db
def test_disabled_discount_codes_setting_rejects_any_submitted_code():
    _make_code()
    config.DISCOUNT_CODES_ENABLED = False

    with pytest.raises(InvalidDiscountCodeError):
        redeem_discount_code(
            "SAVE20",
            subtotal=Decimal("100.00"),
            user=AnonymousUser(),
            phone_number="+233241234567",
        )


@pytest.mark.django_db
def test_an_unknown_code_is_rejected():
    with pytest.raises(InvalidDiscountCodeError):
        redeem_discount_code(
            "NOSUCHCODE",
            subtotal=Decimal("100.00"),
            user=AnonymousUser(),
            phone_number="+233241234567",
        )


@pytest.mark.django_db
def test_an_inactive_code_is_rejected():
    _make_code(is_active=False)

    with pytest.raises(InvalidDiscountCodeError):
        redeem_discount_code(
            "SAVE20",
            subtotal=Decimal("100.00"),
            user=AnonymousUser(),
            phone_number="+233241234567",
        )


@pytest.mark.django_db
def test_an_expired_code_is_rejected():
    today = datetime.date.today()
    _make_code(expiry_date=today - datetime.timedelta(days=1))

    with pytest.raises(InvalidDiscountCodeError):
        redeem_discount_code(
            "SAVE20",
            subtotal=Decimal("100.00"),
            user=AnonymousUser(),
            phone_number="+233241234567",
        )


@pytest.mark.django_db
def test_an_exhausted_code_is_rejected():
    _make_code(max_uses=5, times_used=5)

    with pytest.raises(InvalidDiscountCodeError):
        redeem_discount_code(
            "SAVE20",
            subtotal=Decimal("100.00"),
            user=AnonymousUser(),
            phone_number="+233241234567",
        )


@pytest.mark.django_db
def test_discount_amount_is_capped_by_max_discount_per_order():
    _make_code(
        discount_type=DiscountCode.DiscountType.PERCENTAGE, amount=Decimal("50.00")
    )
    config.MAX_DISCOUNT_PER_ORDER = Decimal("30.00")

    code, amount = redeem_discount_code(
        "SAVE20",
        subtotal=Decimal("200.00"),
        user=AnonymousUser(),
        phone_number="+233241234567",
    )

    # 50% of 200 = 100, but capped at the platform-wide 30 ceiling.
    assert amount == Decimal("30.00")


@pytest.mark.django_db
def test_limit_one_per_customer_blocks_a_repeat_guest_by_phone_number():
    code = _make_code(limit_one_per_customer=True)
    _make_confirmed_order(discount_code=code, phone_number="+233241234567")

    with pytest.raises(InvalidDiscountCodeError):
        redeem_discount_code(
            "SAVE20",
            subtotal=Decimal("100.00"),
            user=AnonymousUser(),
            phone_number="+233241234567",
        )


@pytest.mark.django_db
def test_limit_one_per_customer_blocks_a_repeat_logged_in_account():
    user = User.objects.create_user(username="ama@example.test", password="pw")
    code = _make_code(limit_one_per_customer=True)
    _make_confirmed_order(
        discount_code=code, customer=user, phone_number="+233209990000"
    )

    with pytest.raises(InvalidDiscountCodeError):
        redeem_discount_code(
            "SAVE20",
            subtotal=Decimal("100.00"),
            user=user,
            # A different phone number this time -- the account match alone
            # must still catch it.
            phone_number="+233241234567",
        )


@pytest.mark.django_db
def test_limit_one_per_customer_catches_a_guest_order_reused_after_login():
    """Task 43b doubt-driven-development finding: redeeming as a guest,
    then creating an account and checking out again while logged in, must
    still be caught -- the check has to look at BOTH identity signals
    together, not whichever one the current checkout happens to use."""
    user = User.objects.create_user(username="ama@example.test", password="pw")
    code = _make_code(limit_one_per_customer=True)
    _make_confirmed_order(
        discount_code=code, customer=None, phone_number="+233241234567"
    )

    with pytest.raises(InvalidDiscountCodeError):
        redeem_discount_code(
            "SAVE20",
            subtotal=Decimal("100.00"),
            user=user,
            phone_number="+233241234567",
        )


@pytest.mark.django_db
def test_limit_one_per_customer_ignores_a_pending_order():
    """times_used only increments at payment confirmation -- a PENDING
    order (abandoned checkout, never paid) must never block a retry."""
    code = _make_code(limit_one_per_customer=True)
    _make_confirmed_order(
        discount_code=code, phone_number="+233241234567", status=Order.Status.PENDING
    )

    result_code, amount = redeem_discount_code(
        "SAVE20",
        subtotal=Decimal("100.00"),
        user=AnonymousUser(),
        phone_number="+233241234567",
    )

    assert result_code == code
    assert amount == Decimal("20.00")


@pytest.mark.django_db
def test_limit_one_per_customer_ignores_a_cancelled_order():
    code = _make_code(limit_one_per_customer=True)
    _make_confirmed_order(
        discount_code=code, phone_number="+233241234567", status=Order.Status.CANCELLED
    )

    result_code, _amount = redeem_discount_code(
        "SAVE20",
        subtotal=Decimal("100.00"),
        user=AnonymousUser(),
        phone_number="+233241234567",
    )

    assert result_code == code


@pytest.mark.django_db
def test_limit_one_per_customer_off_allows_a_repeat_redemption():
    code = _make_code(limit_one_per_customer=False)
    _make_confirmed_order(discount_code=code, phone_number="+233241234567")

    result_code, _amount = redeem_discount_code(
        "SAVE20",
        subtotal=Decimal("100.00"),
        user=AnonymousUser(),
        phone_number="+233241234567",
    )

    assert result_code == code


# ---------------------------------------------------------------------------
# Task 43c: audience restriction
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_distributor_only_code_is_rejected_for_a_guest():
    _make_code(audience=DiscountCode.Audience.DISTRIBUTOR)

    with pytest.raises(InvalidDiscountCodeError):
        redeem_discount_code(
            "SAVE20",
            subtotal=Decimal("100.00"),
            user=AnonymousUser(),
            phone_number="+233241234567",
        )


@pytest.mark.django_db
def test_distributor_only_code_is_rejected_for_a_non_distributor_customer():
    user = User.objects.create_user(username="ama@example.test", password="pw")
    _make_code(audience=DiscountCode.Audience.DISTRIBUTOR)

    with pytest.raises(InvalidDiscountCodeError):
        redeem_discount_code(
            "SAVE20",
            subtotal=Decimal("100.00"),
            user=user,
            phone_number="+233241234567",
        )


@pytest.mark.django_db
def test_distributor_only_code_is_redeemable_by_a_distributor():
    distributor_user = _make_distributor_user()
    _make_code(audience=DiscountCode.Audience.DISTRIBUTOR)

    result_code, amount = redeem_discount_code(
        "SAVE20",
        subtotal=Decimal("100.00"),
        user=distributor_user,
        phone_number="+233551234567",
    )

    assert result_code.code == "SAVE20"
    assert amount == Decimal("20.00")


@pytest.mark.django_db
def test_retail_only_code_is_rejected_for_a_distributor():
    distributor_user = _make_distributor_user()
    _make_code(audience=DiscountCode.Audience.RETAIL)

    with pytest.raises(InvalidDiscountCodeError):
        redeem_discount_code(
            "SAVE20",
            subtotal=Decimal("100.00"),
            user=distributor_user,
            phone_number="+233551234567",
        )


@pytest.mark.django_db
def test_retail_only_code_is_redeemable_by_a_guest():
    _make_code(audience=DiscountCode.Audience.RETAIL)

    result_code, _amount = redeem_discount_code(
        "SAVE20",
        subtotal=Decimal("100.00"),
        user=AnonymousUser(),
        phone_number="+233241234567",
    )

    assert result_code.code == "SAVE20"


@pytest.mark.django_db
def test_retail_only_code_is_redeemable_by_a_non_distributor_customer():
    user = User.objects.create_user(username="ama@example.test", password="pw")
    _make_code(audience=DiscountCode.Audience.RETAIL)

    result_code, _amount = redeem_discount_code(
        "SAVE20", subtotal=Decimal("100.00"), user=user, phone_number="+233241234567"
    )

    assert result_code.code == "SAVE20"


@pytest.mark.django_db
def test_everyone_code_is_redeemable_by_a_guest():
    _make_code(audience=DiscountCode.Audience.EVERYONE)

    result_code, _amount = redeem_discount_code(
        "SAVE20",
        subtotal=Decimal("100.00"),
        user=AnonymousUser(),
        phone_number="+233241234567",
    )

    assert result_code.code == "SAVE20"


@pytest.mark.django_db
def test_everyone_code_is_redeemable_by_a_non_distributor_customer():
    user = User.objects.create_user(username="ama@example.test", password="pw")
    _make_code(audience=DiscountCode.Audience.EVERYONE)

    result_code, _amount = redeem_discount_code(
        "SAVE20", subtotal=Decimal("100.00"), user=user, phone_number="+233241234567"
    )

    assert result_code.code == "SAVE20"


@pytest.mark.django_db
def test_everyone_code_is_redeemable_by_a_distributor():
    distributor_user = _make_distributor_user()
    _make_code(audience=DiscountCode.Audience.EVERYONE)

    result_code, _amount = redeem_discount_code(
        "SAVE20",
        subtotal=Decimal("100.00"),
        user=distributor_user,
        phone_number="+233551234567",
    )

    assert result_code.code == "SAVE20"


# ---------------------------------------------------------------------------
# consume_discount_code / release_discount_code
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_consume_discount_code_increments_times_used():
    code = _make_code(times_used=0)

    consume_discount_code(code.pk)

    code.refresh_from_db()
    assert code.times_used == 1


@pytest.mark.django_db
def test_consume_discount_code_can_exceed_max_uses():
    """User-confirmed design (2026-08-13): once payment is captured, the
    order is always honored -- a code exhausted by a concurrent order
    between this order's creation and its own confirmation still gets its
    usage recorded, even past max_uses. Matches real-world platforms
    (Shopify/Stripe/Amazon), which never cancel an already-paid order over
    a promo-code counting race."""
    code = _make_code(max_uses=1, times_used=1)

    consume_discount_code(code.pk)

    code.refresh_from_db()
    assert code.times_used == 2


@pytest.mark.django_db
def test_release_discount_code_decrements_times_used():
    code = _make_code(times_used=3)

    release_discount_code(code.pk)

    code.refresh_from_db()
    assert code.times_used == 2


@pytest.mark.django_db
def test_release_discount_code_never_goes_below_zero():
    code = _make_code(times_used=0)

    release_discount_code(code.pk)

    code.refresh_from_db()
    assert code.times_used == 0
