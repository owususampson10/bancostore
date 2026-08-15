import threading
from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, transaction

import pytest
from constance import config

from apps.compliance.models import EscrowLedger, EscrowTransaction
from apps.compliance.services import credit_escrow, reverse_escrow
from apps.orders.models import Order
from bancostore.concurrency import retry_on_lock_contention

User = get_user_model()
_ref_seq = count(1)


def _make_order(*, subtotal=Decimal("1000.00"), discount_amount=Decimal("0")):
    total = subtotal + Decimal("0") - discount_amount
    return Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=subtotal,
        delivery_fee=Decimal("0"),
        discount_amount=discount_amount,
        total=total,
        payment_reference=f"escrow-test-ref-{next(_ref_seq)}",
        status=Order.Status.CONFIRMED,
    )


# ---------------------------------------------------------------------------
# credit_escrow
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_credit_escrow_credits_the_configured_percentage_of_subtotal():
    order = _make_order(subtotal=Decimal("1000.00"))

    credit_escrow(order)

    ledger = EscrowLedger.objects.get(pk=1)
    assert ledger.balance == Decimal("50.00")  # 5% default rate


@pytest.mark.django_db
def test_credit_escrow_uses_net_revenue_after_discount():
    order = _make_order(subtotal=Decimal("1000.00"), discount_amount=Decimal("100.00"))

    credit_escrow(order)

    ledger = EscrowLedger.objects.get(pk=1)
    assert ledger.balance == Decimal("45.00")  # 5% of (1000 - 100)


@pytest.mark.django_db
def test_credit_escrow_never_includes_delivery_fee():
    order = Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.HOME_DELIVERY,
        delivery_zone=Order.DeliveryZone.ACCRA,
        address="12 High St",
        area="Osu",
        subtotal=Decimal("1000.00"),
        delivery_fee=Decimal("50.00"),
        total=Decimal("1050.00"),
        payment_reference=f"escrow-test-ref-{next(_ref_seq)}",
        status=Order.Status.CONFIRMED,
    )

    credit_escrow(order)

    ledger = EscrowLedger.objects.get(pk=1)
    assert ledger.balance == Decimal("50.00")  # 5% of subtotal only, not 1050


@pytest.mark.django_db
def test_credit_escrow_creates_a_transaction_row_with_a_rate_snapshot():
    order = _make_order(subtotal=Decimal("1000.00"))

    credit_escrow(order)

    txn = EscrowTransaction.objects.get(order_id=order.pk)
    assert txn.transaction_type == EscrowTransaction.TransactionType.CREDIT
    assert txn.amount == Decimal("50.00")
    assert txn.rate_applied == Decimal("5")


@pytest.mark.django_db
def test_credit_escrow_is_a_noop_when_the_rate_is_zero():
    config.ESCROW_RESERVE_RATE = Decimal("0")
    order = _make_order(subtotal=Decimal("1000.00"))

    credit_escrow(order)  # must not raise

    ledger = EscrowLedger.objects.get(pk=1)
    assert ledger.balance == Decimal("0")
    assert not EscrowTransaction.objects.filter(order_id=order.pk).exists()


@pytest.mark.django_db
def test_a_second_order_adds_to_the_existing_balance():
    credit_escrow(_make_order(subtotal=Decimal("1000.00")))
    credit_escrow(_make_order(subtotal=Decimal("2000.00")))

    ledger = EscrowLedger.objects.get(pk=1)
    assert ledger.balance == Decimal("150.00")  # 50 + 100
    assert EscrowTransaction.objects.count() == 2


@pytest.mark.django_db
def test_credit_escrow_rounds_to_the_nearest_pesewa():
    # 5% of 333.33 = 16.6665 -> rounds to 16.67 (ROUND_HALF_UP)
    order = _make_order(subtotal=Decimal("333.33"))

    credit_escrow(order)

    ledger = EscrowLedger.objects.get(pk=1)
    assert ledger.balance == Decimal("16.67")


@pytest.mark.django_db(transaction=True)
def test_concurrent_credits_never_lose_an_update():
    """Mirrors apps/wallet's own
    test_concurrent_credits_never_lose_an_update exactly, for the
    singleton EscrowLedger row instead of a per-distributor Wallet row:
    five concurrent GHS 1000-subtotal orders (each crediting GHS 50 at
    the 5% default rate) must sum to exactly GHS 250, not less."""
    orders = [_make_order(subtotal=Decimal("1000.00")) for _ in range(5)]

    def attempt(order):
        try:
            retry_on_lock_contention(lambda: credit_escrow(order))
        finally:
            connection.close()

    threads = [threading.Thread(target=attempt, args=(order,)) for order in orders]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ledger = EscrowLedger.objects.get(pk=1)
    assert ledger.balance == Decimal("250.00")
    assert EscrowTransaction.objects.count() == 5


# ---------------------------------------------------------------------------
# reverse_escrow
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_reverse_escrow_reverses_the_exact_original_amount():
    order = _make_order(subtotal=Decimal("1000.00"))
    credit_escrow(order)

    reverse_escrow(order.pk)

    ledger = EscrowLedger.objects.get(pk=1)
    assert ledger.balance == Decimal("0.00")
    reversal = EscrowTransaction.objects.get(
        order_id=order.pk, transaction_type=EscrowTransaction.TransactionType.REVERSAL
    )
    assert reversal.amount == Decimal("-50.00")


@pytest.mark.django_db
def test_reverse_escrow_uses_the_rate_snapshot_not_the_live_rate():
    order = _make_order(subtotal=Decimal("1000.00"))
    credit_escrow(order)  # credited at the 5% default

    config.ESCROW_RESERVE_RATE = Decimal("10")  # admin changes the rate afterward
    reverse_escrow(order.pk)

    ledger = EscrowLedger.objects.get(pk=1)
    # Reverses the original GHS 50 (5%), not a recomputed GHS 100 (10%).
    assert ledger.balance == Decimal("0.00")
    reversal = EscrowTransaction.objects.get(
        order_id=order.pk, transaction_type=EscrowTransaction.TransactionType.REVERSAL
    )
    assert reversal.amount == Decimal("-50.00")
    assert reversal.rate_applied == Decimal("5")


@pytest.mark.django_db
def test_reverse_escrow_is_idempotent():
    order = _make_order(subtotal=Decimal("1000.00"))
    credit_escrow(order)

    reverse_escrow(order.pk)
    reverse_escrow(order.pk)  # second call must be a no-op, not a double-reversal

    ledger = EscrowLedger.objects.get(pk=1)
    assert ledger.balance == Decimal("0.00")
    assert (
        EscrowTransaction.objects.filter(
            order_id=order.pk,
            transaction_type=EscrowTransaction.TransactionType.REVERSAL,
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_reverse_escrow_is_a_noop_for_an_order_with_no_credit():
    order = _make_order(subtotal=Decimal("1000.00"))  # never credited

    reverse_escrow(order.pk)  # must not raise

    ledger = EscrowLedger.objects.get(pk=1)
    assert ledger.balance == Decimal("0.00")
    assert not EscrowTransaction.objects.filter(order_id=order.pk).exists()


# ---------------------------------------------------------------------------
# Model-level constraints
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_sign_constraint_rejects_a_negative_credit():
    ledger = EscrowLedger.objects.get(pk=1)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            EscrowTransaction.objects.create(
                ledger=ledger,
                order_id=1,
                transaction_type=EscrowTransaction.TransactionType.CREDIT,
                amount=Decimal("-10.00"),
                rate_applied=Decimal("5"),
            )


@pytest.mark.django_db
def test_sign_constraint_rejects_a_positive_reversal():
    ledger = EscrowLedger.objects.get(pk=1)

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            EscrowTransaction.objects.create(
                ledger=ledger,
                order_id=1,
                transaction_type=EscrowTransaction.TransactionType.REVERSAL,
                amount=Decimal("10.00"),
                rate_applied=Decimal("5"),
            )


@pytest.mark.django_db
def test_unique_constraint_prevents_a_second_credit_for_the_same_order():
    order = _make_order(subtotal=Decimal("1000.00"))
    credit_escrow(order)

    ledger = EscrowLedger.objects.get(pk=1)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            EscrowTransaction.objects.create(
                ledger=ledger,
                order_id=order.pk,
                transaction_type=EscrowTransaction.TransactionType.CREDIT,
                amount=Decimal("50.00"),
                rate_applied=Decimal("5"),
            )
