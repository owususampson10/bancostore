from decimal import Decimal

from django.db import IntegrityError, transaction

import pytest

from apps.catalog.models import Category, Product
from apps.orders.models import Order, OrderItem


def _make_product(**overrides):
    category = Category.objects.create(name="Wellness", slug="wellness")
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


def _make_order(**overrides):
    defaults = {
        "full_name": "Ama Mensah",
        "phone_number": "+233241234567",
        "email": "ama@example.test",
        "delivery_method": Order.DeliveryMethod.HOME_DELIVERY,
        "delivery_zone": Order.DeliveryZone.ACCRA,
        "address": "12 High St",
        "area": "Osu",
        "subtotal": Decimal("450.00"),
        "delivery_fee": Decimal("50.00"),
        "total": Decimal("500.00"),
        "payment_reference": "order-test-ref-1",
    }
    defaults.update(overrides)
    return Order.objects.create(**defaults)


@pytest.mark.django_db
def test_fields_round_trip_correctly():
    order = _make_order()

    reloaded = Order.objects.get(pk=order.pk)
    assert reloaded.full_name == "Ama Mensah"
    assert reloaded.status == Order.Status.PENDING
    assert reloaded.customer_id is None
    assert reloaded.pv_earned == 0


@pytest.mark.django_db
def test_status_choices_include_every_section_5_2_stage():
    values = {choice.value for choice in Order.Status}
    assert values == {
        "pending",
        "confirmed",
        "processing",
        "dispatched",
        "delivered",
        "cancelled",
        "refunded",
    }


@pytest.mark.django_db
def test_order_item_unit_price_and_pv_are_snapshots_not_live_lookups():
    product = _make_product(price=Decimal("450.00"), pv_value=60)
    order = _make_order()
    item = OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        quantity=1,
        unit_price=product.price,
        unit_pv=product.pv_value,
    )

    # Live product price/pv_value change after the order was placed --
    # the already-created OrderItem must not reflect it.
    product.price = Decimal("999.00")
    product.pv_value = 1
    product.save()

    reloaded = OrderItem.objects.get(pk=item.pk)
    assert reloaded.unit_price == Decimal("450.00")
    assert reloaded.unit_pv == 60


@pytest.mark.django_db
def test_delivery_fee_snapshot_does_not_change_if_live_constance_setting_changes():
    from constance import config

    original = config.DELIVERY_FEE_ACCRA
    try:
        config.DELIVERY_FEE_ACCRA = Decimal("50.00")
        order = _make_order(delivery_fee=config.DELIVERY_FEE_ACCRA)

        # Admin changes the live setting after this order was placed.
        config.DELIVERY_FEE_ACCRA = Decimal("999.00")

        reloaded = Order.objects.get(pk=order.pk)
        assert reloaded.delivery_fee == Decimal("50.00")
    finally:
        config.DELIVERY_FEE_ACCRA = original


@pytest.mark.django_db
def test_rejects_amount_mismatch_at_db_level():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _make_order(
                subtotal=Decimal("450.00"),
                delivery_fee=Decimal("50.00"),
                total=Decimal("1000.00"),
            )


@pytest.mark.django_db
def test_rejects_negative_delivery_fee_at_db_level():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _make_order(delivery_fee=Decimal("-1.00"), total=Decimal("449.00"))


@pytest.mark.django_db
def test_rejects_negative_subtotal_at_db_level():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _make_order(subtotal=Decimal("-1.00"), total=Decimal("49.00"))


@pytest.mark.django_db
def test_rejects_negative_total_at_db_level():
    # Only reachable with a negative subtotal/delivery_fee too, since
    # total = subtotal + delivery_fee is separately enforced -- still
    # worth its own test per WithdrawalRequest's identically-shaped
    # constraint precedent (tests/unit/withdrawal/test_withdrawal_request_model.py),
    # which tests every AND'd branch individually after a coarser test
    # once let a real gap in that constraint's shape through.
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _make_order(
                subtotal=Decimal("-500.00"),
                delivery_fee=Decimal("50.00"),
                total=Decimal("-450.00"),
            )


@pytest.mark.django_db
def test_pickup_requires_blank_address_fields_at_db_level():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _make_order(
                delivery_method=Order.DeliveryMethod.PICKUP,
                delivery_zone="",
                address="12 High St",
                area="Osu",
                delivery_fee=Decimal("0.00"),
                total=Decimal("450.00"),
            )


@pytest.mark.django_db
def test_pickup_with_all_address_fields_blank_is_allowed():
    order = _make_order(
        delivery_method=Order.DeliveryMethod.PICKUP,
        delivery_zone="",
        address="",
        area="",
        delivery_fee=Decimal("0.00"),
        total=Decimal("450.00"),
    )
    assert order.pk is not None


@pytest.mark.django_db
def test_pickup_with_a_positive_delivery_fee_is_rejected_at_db_level():
    # CodeRabbit (PR #24): pickup is always free (ADR-0005 decision 1) --
    # a positive delivery_fee on a pickup order must be impossible to
    # persist, not just discouraged by the service layer.
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _make_order(
                delivery_method=Order.DeliveryMethod.PICKUP,
                delivery_zone="",
                address="",
                area="",
                delivery_fee=Decimal("20.00"),
                total=Decimal("470.00"),
            )


@pytest.mark.django_db
def test_home_delivery_requires_all_address_fields_at_db_level():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            _make_order(
                delivery_method=Order.DeliveryMethod.HOME_DELIVERY,
                delivery_zone="",
                address="",
                area="",
            )


@pytest.mark.django_db
def test_order_item_rejects_zero_quantity_at_db_level():
    product = _make_product()
    order = _make_order()
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            OrderItem.objects.create(
                order=order,
                product=product,
                product_name=product.name,
                quantity=0,
                unit_price=product.price,
            )


@pytest.mark.django_db
def test_guest_checkout_has_no_customer():
    order = _make_order()
    assert order.customer is None


@pytest.mark.django_db
def test_email_can_be_blank_for_a_distributor_with_no_real_email():
    order = _make_order(email="", payment_reference="order-test-ref-2")
    assert order.email == ""
