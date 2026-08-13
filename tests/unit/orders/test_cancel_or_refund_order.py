import threading
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection

import pytest

from apps.binary_tree.models import BinaryTreeEdge
from apps.binary_tree.services import BinaryTree
from apps.catalog.models import Category, Product
from apps.distributors.models import Distributor
from apps.orders.models import Order, OrderItem
from apps.orders.services import cancel_or_refund_order, confirm_order_payment
from apps.pv_ledger.models import MonthlyPersonalPv, PvDailyBucket, PvLedger
from apps.pv_ledger.services import consume_leg_pv_fifo
from bancostore.concurrency import select_for_update_nowait_if_supported

User = get_user_model()
_phone_seq = count(1)


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


def _make_distributor(sponsor=None):
    phone = f"+233246{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    Group.objects.get_or_create(name="distributor")[0].user_set.add(user)
    return Distributor.objects.create(user=user, phone_number=phone, sponsor=sponsor)


def _make_order(*, customer=None, total=Decimal("450.00")):
    return Order.objects.create(
        customer=customer,
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=total,
        delivery_fee=Decimal("0"),
        total=total,
        payment_reference=f"order-test-ref-{next(_phone_seq)}",
    )


def _add_item(order, product, *, quantity=1):
    return OrderItem.objects.create(
        order=order,
        product=product,
        product_name=product.name,
        quantity=quantity,
        unit_price=product.price,
        unit_pv=product.pv_value,
    )


@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def _confirm(order, amount_pesewas, mock_verify, mock_sms, mock_mail):
    mock_verify.return_value = {
        "status": "success",
        "amount": amount_pesewas,
        "currency": "GHS",
    }
    confirm_order_payment(order.payment_reference)
    order.refresh_from_db()
    return order


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_cancelling_a_confirmed_order_restores_stock_automatically(mock_sms, mock_mail):
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("900.00"))
    _add_item(order, product, quantity=2)
    order = _confirm(order, 90000)
    product.refresh_from_db()
    assert product.stock == 3  # decremented at confirmation

    cancel_or_refund_order(order.pk, Order.Status.CANCELLED)

    order.refresh_from_db()
    product.refresh_from_db()
    assert order.status == Order.Status.CANCELLED
    assert product.stock == 5


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_to_status_other_than_cancelled_or_refunded_is_rejected(mock_sms, mock_mail):
    """Round-3 doubt-driven-development finding: an earlier draft let ANY
    is_legal_order_status_transition target through, meaning a caller
    mistake (e.g. passing PROCESSING) would reverse a healthy order's
    stock/PV. This must be impossible, not just documented."""
    product = _make_product(stock=5)
    order = _make_order()
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)

    with pytest.raises(ValueError):
        cancel_or_refund_order(order.pk, Order.Status.PROCESSING)

    order.refresh_from_db()
    product.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    assert product.stock == 4


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_refund_requires_an_explicit_restock_argument(mock_sms, mock_mail):
    """No silent default -- matches standard e-commerce practice (e.g.
    Shopify's refund flow requires an explicit 'restock' choice, since a
    refund doesn't always mean the goods physically came back)."""
    product = _make_product(stock=5)
    order = _make_order()
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)

    with pytest.raises(ValueError):
        cancel_or_refund_order(order.pk, Order.Status.REFUNDED)

    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_refund_with_restock_true_restores_stock(mock_sms, mock_mail):
    product = _make_product(stock=5)
    order = _make_order()
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)
    product.refresh_from_db()
    assert product.stock == 4

    cancel_or_refund_order(order.pk, Order.Status.REFUNDED, restock=True)

    order.refresh_from_db()
    product.refresh_from_db()
    assert order.status == Order.Status.REFUNDED
    assert product.stock == 5


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_refund_with_restock_false_does_not_restore_stock(mock_sms, mock_mail):
    """A goodwill refund (or one where the customer keeps a damaged item)
    must not silently inflate Product.stock for units never returned."""
    product = _make_product(stock=5)
    order = _make_order()
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)
    product.refresh_from_db()
    assert product.stock == 4

    cancel_or_refund_order(order.pk, Order.Status.REFUNDED, restock=False)

    order.refresh_from_db()
    product.refresh_from_db()
    assert order.status == Order.Status.REFUNDED
    assert product.stock == 4


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_a_pending_order_is_an_untouched_no_op(mock_sms, mock_mail):
    """This function is for already-paid orders only -- calling it on a
    still-pending order (a caller bug; a separate, existing function owns
    unpaid-order auto-cancel) must not add back stock that was never
    removed."""
    product = _make_product(stock=5)
    order = _make_order()
    _add_item(order, product, quantity=1)
    assert order.status == Order.Status.PENDING

    cancel_or_refund_order(order.pk, Order.Status.CANCELLED)

    order.refresh_from_db()
    product.refresh_from_db()
    assert order.status == Order.Status.PENDING
    assert product.stock == 5


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_an_illegal_transition_is_rejected(mock_sms, mock_mail):
    """DELIVERED -> CANCELLED is not in the legal-transition graph (18a) --
    cancellation is pre-dispatch only."""
    product = _make_product(stock=5)
    order = _make_order()
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)
    order.status = Order.Status.DELIVERED
    order.save(update_fields=["status"])

    with pytest.raises(RuntimeError):
        cancel_or_refund_order(order.pk, Order.Status.CANCELLED)


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_a_duplicate_cancel_attempt_on_an_already_cancelled_order_is_a_no_op(
    mock_sms, mock_mail
):
    product = _make_product(stock=5)
    order = _make_order()
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)

    cancel_or_refund_order(order.pk, Order.Status.CANCELLED)
    product.refresh_from_db()
    assert product.stock == 5

    cancel_or_refund_order(order.pk, Order.Status.CANCELLED)

    product.refresh_from_db()
    assert product.stock == 5  # not double-restored


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_pv_earned_is_zeroed_after_cancellation(mock_sms, mock_mail):
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)
    product = _make_product(stock=5, pv_value=60)
    order = _make_order(customer=distributor.user)
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)
    assert order.pv_earned == 60

    cancel_or_refund_order(order.pk, Order.Status.CANCELLED)

    order.refresh_from_db()
    assert order.pv_earned == 0


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_cancellation_reverses_ancestor_pv_ledger_daily_bucket_and_personal_pv(
    mock_sms, mock_mail
):
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)
    product = _make_product(stock=5, pv_value=60)
    order = _make_order(customer=distributor.user)
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)

    ledger = PvLedger.objects.get(distributor=sponsor)
    assert ledger.right_leg_pv == 60
    bucket = PvDailyBucket.objects.get(distributor=sponsor)
    assert bucket.pv == 60
    personal = MonthlyPersonalPv.objects.get(distributor=distributor)
    assert personal.pv == 60

    cancel_or_refund_order(order.pk, Order.Status.CANCELLED)

    ledger.refresh_from_db()
    bucket.refresh_from_db()
    personal.refresh_from_db()
    assert ledger.right_leg_pv == 0
    assert bucket.pv == 0
    assert personal.pv == 0


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_reversal_targets_the_original_confirmation_month_not_todays(
    mock_sms, mock_mail
):
    """The date-drift fix must actually be used end-to-end: cancelling
    weeks after confirmation must still reverse the PvDailyBucket/
    MonthlyPersonalPv rows dated at CONFIRMATION time, never "today"."""
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)
    product = _make_product(stock=5, pv_value=60)
    order = _make_order(customer=distributor.user)
    _add_item(order, product, quantity=1)

    with patch("django.utils.timezone.now") as mock_now:
        mock_now.return_value = datetime(2026, 5, 3, tzinfo=dt_timezone.utc)
        order = _confirm(order, 45000)

    bucket = PvDailyBucket.objects.get(distributor=sponsor)
    assert bucket.date == date(2026, 5, 3)
    personal = MonthlyPersonalPv.objects.get(distributor=distributor)
    assert personal.period == date(2026, 5, 1)

    # Cancelling "now" (whatever the real current date is) must still
    # reverse May's rows, not create/touch a row for today's month.
    cancel_or_refund_order(order.pk, Order.Status.CANCELLED)

    bucket.refresh_from_db()
    personal.refresh_from_db()
    assert bucket.pv == 0
    assert personal.pv == 0
    assert PvDailyBucket.objects.filter(distributor=sponsor).count() == 1
    assert MonthlyPersonalPv.objects.filter(distributor=distributor).count() == 1


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_refunding_a_delivered_order_reverses_stock_and_pv(mock_sms, mock_mail):
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)
    product = _make_product(stock=5, pv_value=60)
    order = _make_order(customer=distributor.user)
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)
    order.status = Order.Status.DELIVERED
    order.save(update_fields=["status"])
    product.refresh_from_db()
    assert product.stock == 4

    cancel_or_refund_order(order.pk, Order.Status.REFUNDED, restock=True)

    order.refresh_from_db()
    product.refresh_from_db()
    ledger = PvLedger.objects.get(distributor=sponsor)
    assert order.status == Order.Status.REFUNDED
    assert order.pv_earned == 0
    assert product.stock == 5
    assert ledger.right_leg_pv == 0


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_restock_argument_is_ignored_for_cancellation(mock_sms, mock_mail):
    """Cancellation is pre-dispatch only, so goods never shipped -- it
    always restocks regardless of whatever `restock` value is passed."""
    product = _make_product(stock=5)
    order = _make_order()
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)

    cancel_or_refund_order(order.pk, Order.Status.CANCELLED, restock=False)

    product.refresh_from_db()
    assert product.stock == 5


@pytest.mark.django_db(transaction=True)
def test_concurrent_binary_bonus_consumption_and_reversal_never_drive_pv_negative():
    """Task 18b's core doubt-driven-development finding (round 3): a
    reversal must lock the same ancestor Distributor row
    process_binary_bonus_for_distributor locks before consume_leg_pv_fifo
    reads/writes PvDailyBucket, or the two can interleave and drive `pv`
    below the real pv__gte=0 CheckConstraint. This only genuinely proves
    the locking itself once run against real MySQL in CI (SQLite drops
    FOR UPDATE, per every other concurrency test's own established
    caveat in this codebase) -- kept anyway since it still catches a
    regression in the surrounding lock/transaction structure on SQLite."""
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)
    product = _make_product(stock=5, pv_value=100)
    order = _make_order(customer=distributor.user, total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    with (
        patch("apps.orders.services.send_mail"),
        patch("apps.orders.services.send_sms"),
    ):
        order = _confirm(order, 45000)

    bucket = PvDailyBucket.objects.get(distributor=sponsor)
    assert bucket.pv == 100
    purchase_date = order.confirmed_at.date()

    def cancel_it():
        with (
            patch("apps.orders.services.send_mail"),
            patch("apps.orders.services.send_sms"),
        ):
            cancel_or_refund_order(order.pk, Order.Status.CANCELLED)
        connection.close()

    def consume_it():
        def _attempt():
            from django.db import transaction

            with transaction.atomic():
                select_for_update_nowait_if_supported(
                    Distributor.objects.filter(pk=sponsor.pk)
                ).get()
                consume_leg_pv_fifo(
                    sponsor,
                    BinaryTreeEdge.Leg.RIGHT,
                    purchase_date,
                    60,
                    "test-binary-bonus-ref",
                )

        from bancostore.concurrency import retry_on_lock_contention

        try:
            retry_on_lock_contention(_attempt)
        except RuntimeError:
            # consume_leg_pv_fifo's own, pre-existing, correct behavior
            # when asked to consume more than is currently available --
            # can legitimately happen here if cancel_it's reversal won
            # the lock race first, leaving nothing left to consume. Not
            # the thing under test; the thing under test is that this
            # can never observe/produce a NEGATIVE bucket value.
            pass
        connection.close()

    threads = [threading.Thread(target=cancel_it), threading.Thread(target=consume_it)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    bucket.refresh_from_db()
    assert bucket.pv >= 0


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_whitespace_only_tracking_note_does_not_clear_an_existing_one(
    mock_sms, mock_mail
):
    """Same bug class CodeRabbit caught in Task 18c's advance_order_status
    (PR #30): a whitespace-only string is truthy in Python, so a naive
    `if tracking_note:` check would persist "   " as if it were a real
    note, silently overwriting a genuine existing one."""
    product = _make_product(stock=5)
    order = _make_order()
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)
    order.tracking_note = "Customer requested cancellation."
    order.save(update_fields=["tracking_note"])

    cancel_or_refund_order(order.pk, Order.Status.CANCELLED, tracking_note="   ")

    order.refresh_from_db()
    assert order.tracking_note == "Customer requested cancellation."


# ---------------------------------------------------------------------------
# Task 43b: discount code slot reversal
# ---------------------------------------------------------------------------


def _make_discount_code(**overrides):
    from datetime import date, timedelta

    from apps.promotions.models import DiscountCode

    defaults = {
        "code": "SAVE20",
        "discount_type": DiscountCode.DiscountType.FIXED,
        "amount": Decimal("20.00"),
        "expiry_date": date.today() + timedelta(days=30),
        "max_uses": 100,
    }
    defaults.update(overrides)
    return DiscountCode.objects.create(**defaults)


def _make_discounted_order(code, total=Decimal("430.00")):
    order = _make_order(total=total)
    order.discount_code = code
    order.discount_amount = Decimal("20.00")
    order.subtotal = total + Decimal("20.00")
    order.save(update_fields=["discount_code", "discount_amount", "subtotal"])
    return order


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_cancelling_a_confirmed_discounted_order_frees_the_usage_slot(
    mock_sms, mock_mail
):
    """Symmetric with consume_discount_code at confirmation -- mirrors
    increment_stock/decrement_stock's own established symmetry. A
    doubt-driven-development finding before this was written: without
    this reversal, every cancelled/refunded order that used a code
    permanently wastes a slot from max_uses with no way to recover it."""
    code = _make_discount_code(times_used=0)
    product = _make_product(stock=5)
    order = _make_discounted_order(code)
    _add_item(order, product, quantity=1)
    order = _confirm(order, 43000)
    code.refresh_from_db()
    assert code.times_used == 1  # consumed at confirmation

    cancel_or_refund_order(order.pk, Order.Status.CANCELLED)

    code.refresh_from_db()
    assert code.times_used == 0


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_refunding_a_confirmed_discounted_order_frees_the_usage_slot(
    mock_sms, mock_mail
):
    code = _make_discount_code(times_used=0)
    product = _make_product(stock=5)
    order = _make_discounted_order(code)
    _add_item(order, product, quantity=1)
    order = _confirm(order, 43000)
    code.refresh_from_db()
    assert code.times_used == 1  # consumed at confirmation

    cancel_or_refund_order(order.pk, Order.Status.REFUNDED, restock=True)

    code.refresh_from_db()
    assert code.times_used == 0


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_cancelling_an_order_with_no_discount_code_touches_no_discount_code(
    mock_sms, mock_mail
):
    code = _make_discount_code(times_used=3)
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    order = _confirm(order, 45000)

    cancel_or_refund_order(order.pk, Order.Status.CANCELLED)

    code.refresh_from_db()
    assert code.times_used == 3  # unrelated code, untouched


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
def test_freeing_a_discount_slot_never_goes_below_zero(mock_sms, mock_mail):
    """Defensive floor, matching this codebase's established
    times_used__gt=0-filtered update convention -- guards against any
    future double-cancel/idempotency edge case, not reachable through the
    normal single-call path today."""
    from apps.promotions.services import release_discount_code

    code = _make_discount_code(times_used=0)

    release_discount_code(code.pk)

    code.refresh_from_db()
    assert code.times_used == 0
