from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection

import pytest
from constance import config

from apps.binary_tree.models import BinaryTreeEdge
from apps.binary_tree.services import BinaryTree
from apps.catalog.models import Category, Product
from apps.compliance.models import ComplianceAlertState, EscrowLedger, EscrowTransaction
from apps.distributors.models import Distributor
from apps.distributors.paystack import PaystackError
from apps.orders.models import Order, OrderItem
from apps.orders.services import confirm_order_payment
from apps.pv_ledger.models import PvDailyBucket, PvLedger

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
    phone = f"+233244{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    Group.objects.get_or_create(name="distributor")[0].user_set.add(user)
    return Distributor.objects.create(user=user, phone_number=phone, sponsor=sponsor)


def _make_customer_user():
    phone = f"+233245{next(_phone_seq):06d}"
    return User.objects.create_user(username=phone, password="Passw0rd!")


def _make_order(
    *,
    customer=None,
    email="ama@example.test",
    total=Decimal("450.00"),
    backorders_allowed_at_checkout=False,
):
    return Order.objects.create(
        customer=customer,
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email=email,
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=total,
        delivery_fee=Decimal("0"),
        total=total,
        payment_reference=f"order-test-ref-{next(_phone_seq)}",
        backorders_allowed_at_checkout=backorders_allowed_at_checkout,
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


def _success_verify(amount, currency="GHS"):
    return {"status": "success", "amount": amount, "currency": currency}


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_guest_order_confirms_and_never_credits_pv(mock_verify, mock_sms, mock_mail):
    product = _make_product(stock=5)
    order = _make_order(customer=None, total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    assert order.confirmed_at is not None
    assert order.pv_earned == 0
    product.refresh_from_db()
    assert product.stock == 4


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_guest_order_still_credits_escrow_despite_never_crediting_pv(
    mock_verify, mock_sms, mock_mail
):
    """Task 47a. A doubt-driven-development finding against an earlier
    draft: escrow is "product revenue" for EVERY confirmed order, not
    just distributor purchases -- an earlier draft would have placed the
    credit_escrow() call inside the distributor-only PV-credit block
    above, silently escrowing nothing for guest/customer orders. This is
    the guest case (no PV credited at all, per the sibling test above),
    proving escrow doesn't share that restriction."""
    product = _make_product(stock=5)
    order = _make_order(customer=None, total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.pv_earned == 0  # confirms this really is the no-PV case
    ledger = EscrowLedger.objects.get(pk=1)
    assert ledger.balance == Decimal("22.50")  # 5% default rate of 450
    txn = EscrowTransaction.objects.get(order_id=order.pk)
    assert txn.transaction_type == EscrowTransaction.TransactionType.CREDIT
    assert txn.amount == Decimal("22.50")


@pytest.mark.django_db
@patch("apps.compliance.services.send_mail")
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_confirming_an_order_that_drops_the_ratio_below_threshold_fires_an_alert(
    mock_verify, mock_sms, mock_order_mail, mock_compliance_mail
):
    """Task 47b. apps.orders.services.send_mail (order confirmation
    emails) and apps.compliance.services.send_mail (the compliance
    alert) are two independent module-level references to
    django.core.mail.send_mail -- mocking one does not mock the other,
    so this test proves the real end-to-end wiring, not just that
    confirming an order doesn't crash."""
    config.COMPLIANCE_ALERT_EMAIL = "compliance@bancostore.test"
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)
    product = _make_product(stock=5, pv_value=60)
    # A prior distributor order already tips the ratio below 70% once
    # this new one confirms too (0% retail among 2 paid orders).
    _make_order(customer=distributor.user, total=Decimal("450.00"))
    Order.objects.filter(customer=distributor.user).update(
        status=Order.Status.CONFIRMED, pv_earned=60
    )
    order = _make_order(customer=distributor.user, total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    mock_compliance_mail.assert_called_once()
    state = ComplianceAlertState.objects.get(pk=1)
    assert state.is_below_threshold is True


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_logged_in_customer_group_order_never_credits_pv(
    mock_verify, mock_sms, mock_mail
):
    user = _make_customer_user()
    product = _make_product(stock=5)
    order = _make_order(customer=user, total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    assert order.pv_earned == 0


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_placed_distributor_order_credits_purchase_and_personal_pv(
    mock_verify, mock_sms, mock_mail
):
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)
    product = _make_product(stock=5, pv_value=60)
    order = _make_order(customer=distributor.user, total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    assert order.pv_earned == 60
    ledger = PvLedger.objects.get(distributor=sponsor)
    assert ledger.right_leg_pv == 60


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_confirmed_at_date_always_matches_the_pv_daily_bucket_it_credited(
    mock_verify, mock_sms, mock_mail
):
    """Task 18b (doubt-driven-development, round 3): record_purchase_pv's
    own internal `timezone.now().date()` call and confirm_order_payment's
    `order.confirmed_at = timezone.now()` are two independent calls with
    real work (a retry loop with sleep) happening between them -- across a
    real midnight boundary they could disagree, and a later cancel/refund
    reversal targeting PvDailyBucket via order.confirmed_at.date() would
    then target the WRONG day's bucket. Simulated here by patching
    django.utils.timezone.now (the one function both apps.orders.services
    and apps.pv_ledger.services import and call) ONLY around the
    confirm_order_payment call itself -- not around setup, which needs its
    own real/unpatched timestamps for auto_now_add fields -- with a
    side_effect returning a different date on each of its first two
    calls. Under the old, buggy call order (record_purchase_pv's internal
    call happens before order.confirmed_at is set), this reproduces
    exactly the drift the fix closes: confirm_order_payment must capture
    `now` once and reuse it, so apps.pv_ledger.services never calls
    timezone.now() a second, differently-dated time for this same
    confirmation."""
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)
    product = _make_product(stock=5, pv_value=60)
    order = _make_order(customer=distributor.user, total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    call_count = {"n": 0}

    def _now_side_effect():
        call_count["n"] += 1
        # Only the very first call (record_purchase_pv's internal "today"
        # under the old, buggy code) sees day 1 -- every other call,
        # however many there turn out to be (confirmed_at, simple_history,
        # or anything else), sees day 2. This models real time passing
        # between the first PV-related date computation and everything
        # after it, without depending on an exact incidental call count.
        if call_count["n"] == 1:
            return datetime(2026, 7, 1, tzinfo=dt_timezone.utc)
        return datetime(2026, 7, 2, tzinfo=dt_timezone.utc)

    with patch("django.utils.timezone.now") as mock_now:
        mock_now.side_effect = _now_side_effect
        confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    bucket = PvDailyBucket.objects.get(distributor=sponsor)
    assert bucket.date == order.confirmed_at.date()


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_unplaced_distributor_order_confirms_but_credits_zero_pv(
    mock_verify, mock_sms, mock_mail
):
    # Doubt-driven-development finding: a distributor with no sponsor/
    # placement (unreachable in the normal onboarding flow, but not
    # database-guaranteed) must never have pv_earned claim a credit that
    # record_purchase_pv silently no-op'd on -- pv_earned must reflect
    # what was ACTUALLY credited, not the pre-computed amount.
    distributor = _make_distributor()
    product = _make_product(stock=5, pv_value=60)
    order = _make_order(customer=distributor.user, total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    assert order.pv_earned == 0
    assert not PvLedger.objects.exists()


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_multiple_line_items_each_decrement_their_own_product_stock(
    mock_verify, mock_sms, mock_mail
):
    product_a = _make_product(name="A", stock=5)
    product_b = _make_product(name="B", stock=3)
    order = _make_order(total=Decimal("900.00"))
    _add_item(order, product_a, quantity=2)
    _add_item(order, product_b, quantity=1)
    mock_verify.return_value = _success_verify(amount=90000)

    confirm_order_payment(order.payment_reference)

    product_a.refresh_from_db()
    product_b.refresh_from_db()
    assert product_a.stock == 3
    assert product_b.stock == 2


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_already_confirmed_order_is_an_idempotent_no_op(
    mock_verify, mock_sms, mock_mail
):
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)
    confirm_order_payment(order.payment_reference)

    product.refresh_from_db()
    assert product.stock == 4  # decremented once, not twice
    assert mock_verify.call_count == 1  # second call short-circuited before verifying


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_cancelled_order_confirmation_attempt_is_a_no_op(
    mock_verify, mock_sms, mock_mail
):
    product = _make_product(stock=0)
    order = _make_order(total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)  # cancels (insufficient stock)
    order.refresh_from_db()
    assert order.status == Order.Status.CANCELLED

    mock_verify.reset_mock()
    confirm_order_payment(order.payment_reference)  # a late duplicate webhook

    order.refresh_from_db()
    assert order.status == Order.Status.CANCELLED
    mock_verify.assert_not_called()


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_non_success_paystack_status_does_not_confirm_the_order(
    mock_verify, mock_sms, mock_mail
):
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = {"status": "failed", "amount": 45000, "currency": "GHS"}

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.status == Order.Status.PENDING
    product.refresh_from_db()
    assert product.stock == 5


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_wrong_currency_does_not_confirm_the_order(mock_verify, mock_sms, mock_mail):
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000, currency="NGN")

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.status == Order.Status.PENDING


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_amount_mismatch_does_not_confirm_the_order(mock_verify, mock_sms, mock_mail):
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=1)  # far too little

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.status == Order.Status.PENDING


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_paystack_verify_error_is_a_safe_no_op(mock_verify, mock_sms, mock_mail):
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.side_effect = PaystackError("network error")

    confirm_order_payment(order.payment_reference)  # must not raise

    order.refresh_from_db()
    assert order.status == Order.Status.PENDING


@pytest.mark.django_db
def test_no_matching_order_is_a_safe_no_op():
    confirm_order_payment("order-does-not-exist")  # must not raise


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_insufficient_stock_cancels_the_order_instead_of_confirming(
    mock_verify, mock_sms, mock_mail
):
    product = _make_product(stock=0)
    order = _make_order(total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)  # must not raise

    order.refresh_from_db()
    assert order.status == Order.Status.CANCELLED
    assert order.pv_earned == 0


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_insufficient_stock_on_one_line_item_does_not_decrement_the_other(
    mock_verify, mock_sms, mock_mail
):
    product_a = _make_product(name="A", stock=5)
    product_b = _make_product(name="B", stock=0)  # will fail
    order = _make_order(total=Decimal("900.00"))
    _add_item(order, product_a, quantity=1)
    _add_item(order, product_b, quantity=1)
    mock_verify.return_value = _success_verify(amount=90000)

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.status == Order.Status.CANCELLED
    product_a.refresh_from_db()
    assert product_a.stock == 5  # rolled back, not partially decremented


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_insufficient_stock_does_not_credit_pv_for_a_distributor_order(
    mock_verify, mock_sms, mock_mail
):
    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.LEFT)
    product = _make_product(stock=0, pv_value=60)
    order = _make_order(customer=distributor.user, total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.status == Order.Status.CANCELLED
    assert order.pv_earned == 0
    ledger = PvLedger.objects.filter(distributor=sponsor).first()
    assert ledger is None or ledger.left_leg_pv == 0


# ---------------------------------------------------------------------------
# Task 44b: backorders_allowed_at_checkout confirms instead of cancelling
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_backordered_item_confirms_instead_of_cancelling(
    mock_verify, mock_sms, mock_mail
):
    product = _make_product(stock=0)
    order = _make_order(total=Decimal("450.00"), backorders_allowed_at_checkout=True)
    item = _add_item(order, product, quantity=3)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    item.refresh_from_db()
    product.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    assert product.stock == 0  # clamped at zero, never negative
    assert item.stock_decremented == 0  # nothing was actually on hand


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_backordered_item_with_partial_stock_decrements_only_what_exists(
    mock_verify, mock_sms, mock_mail
):
    product = _make_product(stock=2)
    order = _make_order(total=Decimal("450.00"), backorders_allowed_at_checkout=True)
    item = _add_item(order, product, quantity=5)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    item.refresh_from_db()
    product.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    assert product.stock == 0
    assert item.stock_decremented == 2  # only what was actually on hand


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_fully_stocked_item_on_a_backorder_allowed_order_decrements_normally(
    mock_verify, mock_sms, mock_mail
):
    """backorders_allowed_at_checkout only ever matters when stock falls
    short -- a normal, fully-available item on the same order behaves
    exactly as before."""
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("450.00"), backorders_allowed_at_checkout=True)
    item = _add_item(order, product, quantity=2)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    product.refresh_from_db()
    item.refresh_from_db()
    assert product.stock == 3
    assert item.stock_decremented == 2


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_insufficient_stock_still_cancels_when_backorders_not_allowed_at_checkout(
    mock_verify, mock_sms, mock_mail
):
    """Regression guard for 44b's own acceptance criteria: an order NOT
    snapshotted as backorder-allowed at checkout must hit the exact same
    cancel path as before this task, even if an admin later turns
    backorders on globally -- confirm_order_payment must never re-derive
    this from a live setting."""
    original_enabled = config.BACKORDERS_ENABLED
    original_behaviour = config.OUT_OF_STOCK_BEHAVIOUR
    config.BACKORDERS_ENABLED = True
    config.OUT_OF_STOCK_BEHAVIOUR = "backorder"
    try:
        product = _make_product(stock=0)
        order = _make_order(total=Decimal("450.00"))  # snapshotted False
        _add_item(order, product, quantity=1)
        mock_verify.return_value = _success_verify(amount=45000)

        confirm_order_payment(order.payment_reference)

        order.refresh_from_db()
        assert order.status == Order.Status.CANCELLED
    finally:
        config.BACKORDERS_ENABLED = original_enabled
        config.OUT_OF_STOCK_BEHAVIOUR = original_behaviour


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_confirmation_sends_sms_and_email_when_email_is_present(
    mock_verify, mock_sms, mock_mail
):
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("450.00"), email="ama@example.test")
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    mock_sms.assert_called_once()
    mock_mail.assert_called_once()


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_confirmation_skips_email_when_order_email_is_blank(
    mock_verify, mock_sms, mock_mail
):
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("450.00"), email="")
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    mock_sms.assert_called_once()
    mock_mail.assert_not_called()


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_sms_failure_does_not_undo_an_already_applied_confirmation(
    mock_verify, mock_sms, mock_mail
):
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)
    mock_sms.side_effect = Exception("SMS provider outage")

    confirm_order_payment(order.payment_reference)  # must not raise

    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    product.refresh_from_db()
    assert product.stock == 4
    mock_mail.assert_called_once()  # the outage didn't stop the email attempt either


@pytest.mark.django_db(transaction=True)
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_concurrent_confirmation_attempts_never_double_decrement_or_double_credit_pv(
    mock_verify, mock_sms, mock_mail
):
    """Debugging-and-error-recovery (2026-07-25): originally written with
    5 threads racing the same reference, matching this codebase's own
    apps/wallet 5-thread convention -- but that test's correctness comes
    from a race-free bulk F() update, not from row locking actually
    resolving contention. confirm_order_payment's idempotency instead
    depends on select_for_update_nowait_if_supported, which SQLite has no
    real implementation of at all (has_select_for_update is False --
    see tests/unit/distributors/test_consume_paid_starter_pack.py::
    test_concurrent_confirmations_under_the_same_sponsor_do_not_lose_pv's
    own docstring, the closest existing precedent for this exact lock
    shape, which deliberately uses only 2 threads for the same reason).
    5-way contention on one row intermittently exhausted
    retry_on_lock_contention's bounded retries under SQLite's coarse
    whole-table locking (reproduced 2/3 local runs, even after adding
    connection.close() per thread) -- reduced to 2 threads to match that
    precedent's own choice, which still genuinely exercises the race
    (both threads target the exact same PENDING order) without depending
    on contention resolution SQLite cannot really provide. Full
    reliability under real concurrent load is what CI's real MySQL run
    verifies, per CLAUDE.md's own testing-strategy rationale -- this
    local test's job is to catch a regression in the retry/lock wiring,
    not to prove SQLite-level concurrency guarantees it cannot make."""
    import threading

    sponsor = _make_distributor()
    distributor = _make_distributor()
    BinaryTree.place_distributor(sponsor, distributor, leg=BinaryTreeEdge.Leg.RIGHT)
    product = _make_product(stock=5, pv_value=60)
    order = _make_order(customer=distributor.user, total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    errors = []

    def _attempt():
        try:
            confirm_order_payment(order.payment_reference)
        except Exception as exc:  # pragma: no cover - captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()  # each thread must not share the main
            # thread's connection/transaction state, matching
            # test_consume_paid_starter_pack.py's own established pattern

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    order.refresh_from_db()
    product.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    assert order.pv_earned == 60
    assert product.stock == 4  # decremented exactly once across both concurrent calls
    ledger = PvLedger.objects.get(distributor=sponsor)
    assert ledger.right_leg_pv == 60  # credited exactly once


# ---------------------------------------------------------------------------
# Task 43b: discount code consumption
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


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_confirming_an_order_with_a_discount_code_increments_times_used(
    mock_verify, mock_sms, mock_mail
):
    code = _make_discount_code(times_used=0)
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("430.00"))
    order.discount_code = code
    order.discount_amount = Decimal("20.00")
    order.subtotal = Decimal("450.00")
    order.save(update_fields=["discount_code", "discount_amount", "subtotal"])
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=43000)

    confirm_order_payment(order.payment_reference)

    code.refresh_from_db()
    assert code.times_used == 1


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_confirming_an_order_with_no_discount_code_touches_no_discount_code(
    mock_verify, mock_sms, mock_mail
):
    code = _make_discount_code(times_used=0)
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("450.00"))
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=45000)

    confirm_order_payment(order.payment_reference)

    code.refresh_from_db()
    assert code.times_used == 0


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_confirming_an_order_honors_a_discount_code_exhausted_after_creation(
    mock_verify, mock_sms, mock_mail
):
    """User-confirmed design (2026-08-13): once payment is captured, the
    order is always honored, even if a concurrent order already claimed
    the code's last usage slot between this order's creation and its own
    confirmation. Matches real-world platforms (Shopify/Stripe/Amazon),
    which never cancel an already-paid order over a promo-code race."""
    code = _make_discount_code(max_uses=1, times_used=1)  # already exhausted
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("430.00"))
    order.discount_code = code
    order.discount_amount = Decimal("20.00")
    order.subtotal = Decimal("450.00")
    order.save(update_fields=["discount_code", "discount_amount", "subtotal"])
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=43000)

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    code.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    assert code.times_used == 2  # honored past max_uses, not rejected


@pytest.mark.django_db
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_confirming_an_order_honors_a_discount_code_disabled_after_creation(
    mock_verify, mock_sms, mock_mail
):
    """User-confirmed design (2026-08-13): an admin disabling a code only
    stops it applying to NEW checkouts -- it does not retroactively
    revalidate is_active for an order that already has it snapshotted."""
    code = _make_discount_code()
    product = _make_product(stock=5)
    order = _make_order(total=Decimal("430.00"))
    order.discount_code = code
    order.discount_amount = Decimal("20.00")
    order.subtotal = Decimal("450.00")
    order.save(update_fields=["discount_code", "discount_amount", "subtotal"])
    _add_item(order, product, quantity=1)
    mock_verify.return_value = _success_verify(amount=43000)

    code.is_active = False
    code.save(update_fields=["is_active"])

    confirm_order_payment(order.payment_reference)

    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    assert order.discount_amount == Decimal("20.00")


@pytest.mark.django_db(transaction=True)
@patch("apps.orders.services.send_mail")
@patch("apps.orders.services.send_sms")
@patch("apps.orders.services.verify_transaction")
def test_concurrent_order_confirmations_never_lose_a_discount_code_usage_count(
    mock_verify, mock_sms, mock_mail
):
    """Mirrors apps/wallet's own concurrent-credits test: the times_used
    increment happens via a bulk F() update entirely inside one DB
    statement, so it's race-free without needing select_for_update on
    DiscountCode itself. Two distinct orders, each using the same code,
    confirmed concurrently -- times_used must land at exactly 2.

    2 threads, not 5 -- matching this file's own established precedent
    just above (test_concurrent_confirmation_attempts_never_double_
    decrement_or_double_credit_pv's docstring): SQLite has no real
    concurrent-write support at all (a single whole-database write lock,
    not per-row), so 5 truly concurrent writers -- even across
    independent Order/Product rows -- reliably exhausts
    retry_on_lock_contention's bounded retries here, independent of
    whether the DiscountCode update itself is race-free. This test's job
    is to catch a regression in the F()-update wiring, not to prove
    SQLite-level concurrency guarantees it cannot make; real concurrent
    load is what CI's MySQL run verifies.

    The mocks are patched once, outside the threads (matching this file's
    own established convention just above) -- unittest.mock.patch is not
    itself thread-safe to apply/remove concurrently from multiple
    threads, so each thread must reuse the same already-applied mock
    rather than opening its own `with patch(...)` block."""
    import threading

    mock_verify.return_value = _success_verify(amount=43000)
    code = _make_discount_code(times_used=0)
    orders = []
    for i in range(2):
        product = _make_product(stock=5, name=f"Concurrent Product {i}")
        order = _make_order(total=Decimal("430.00"))
        order.discount_code = code
        order.discount_amount = Decimal("20.00")
        order.subtotal = Decimal("450.00")
        order.save(update_fields=["discount_code", "discount_amount", "subtotal"])
        _add_item(order, product, quantity=1)
        orders.append(order)

    errors = []

    def _attempt(order):
        try:
            confirm_order_payment(order.payment_reference)
        except Exception as exc:  # pragma: no cover - captured for the assertion below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=_attempt, args=(order,)) for order in orders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    code.refresh_from_db()
    assert code.times_used == 2
