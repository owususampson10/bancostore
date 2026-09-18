"""Task 68f. Money going back to a customer on Paystack's side.

Before this, nothing listened: an order stayed confirmed with its stock
gone, its PV credited and the sponsor's bonus paid, while the money had
already left the merchant account.
"""

from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone

import pytest

from apps.distributors.external_refunds import (
    handle_external_refund,
    handle_payment_dispute,
)
from apps.distributors.models import Distributor, PaymentIssue
from apps.distributors.payment_outcomes import PaymentOutcome
from apps.distributors.paystack import PaystackError
from apps.orders.models import Order

User = get_user_model()
_seq = count(1)
VERIFY = "apps.distributors.external_refunds.verify_transaction"


@pytest.fixture(autouse=True)
def _run_on_commit_now(monkeypatch):
    monkeypatch.setattr(
        "apps.distributors.payment_issues.transaction.on_commit", lambda fn: fn()
    )


def _reversed_verify():
    return {"status": "reversed", "amount": 45000, "currency": "GHS"}


def _make_order(reference, **fields):
    fields.setdefault("status", Order.Status.CONFIRMED)
    return Order.objects.create(
        full_name="Ama Mensah",
        phone_number="+233241234567",
        email="ama@example.test",
        delivery_method=Order.DeliveryMethod.PICKUP,
        subtotal=Decimal("450.00"),
        delivery_fee=Decimal("0"),
        total=Decimal("450.00"),
        payment_reference=reference,
        **fields,
    )


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_a_refund_is_confirmed_with_paystack_not_taken_on_trust(mock_verify):
    """A webhook body is never the source of truth here, the same rule every
    other payment path follows."""
    mock_verify.return_value = {"status": "success"}
    _make_order("order-abc")

    outcome = handle_external_refund("order-abc")

    assert outcome == PaymentOutcome.NOT_PAID
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_a_refunded_order_is_marked_refunded_and_recorded(mock_verify):
    """The real reversal runs here, not a mock: the detail the admin reads is
    built from the order's status AFTERWARDS, so mocking it away would test
    the wrong branch (adversarial security review)."""
    mock_verify.return_value = _reversed_verify()
    order = _make_order("order-abc", confirmed_at=timezone.now())

    with (
        patch("apps.orders.services.send_sms"),
        patch("apps.orders.services.send_mail"),
    ):
        outcome = handle_external_refund("order-abc")

    assert outcome == PaymentOutcome.ISSUE_RECORDED
    order.refresh_from_db()
    assert order.status == Order.Status.REFUNDED
    issue = PaymentIssue.objects.get(reference="order-abc")
    assert issue.kind == PaymentIssue.Kind.REFUND_RECEIVED
    assert "stock was not put back" in issue.detail.lower()


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_an_order_that_could_not_be_reversed_says_so_plainly(mock_verify):
    """cancel_or_refund_order is a quiet no-op for an order that was never
    confirmed. Claiming "its PV was reversed" would send the admin looking
    for a reversal that never happened."""
    mock_verify.return_value = _reversed_verify()
    _make_order("order-abc", status=Order.Status.PENDING)

    handle_external_refund("order-abc")

    detail = PaymentIssue.objects.get(reference="order-abc").detail
    assert "could not be marked refunded" in detail
    assert "pending" in detail


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_an_order_already_refunded_is_left_alone(mock_verify):
    mock_verify.return_value = _reversed_verify()
    _make_order("order-abc", status=Order.Status.REFUNDED)

    assert handle_external_refund("order-abc") == PaymentOutcome.ALREADY_APPLIED


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_a_refund_we_cannot_verify_asks_paystack_to_retry(mock_verify):
    mock_verify.side_effect = PaystackError("timed out")

    assert handle_external_refund("order-abc") == PaymentOutcome.VERIFY_FAILED
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_a_refunded_payment_with_no_order_is_still_recorded(mock_verify):
    mock_verify.return_value = _reversed_verify()

    assert handle_external_refund("order-gone") == PaymentOutcome.ISSUE_RECORDED
    assert PaymentIssue.objects.filter(reference="order-gone").exists()


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_a_refunded_registration_fee_is_reported_for_a_human(mock_verify):
    mock_verify.return_value = _reversed_verify()

    outcome = handle_external_refund("reg-abc")

    assert outcome == PaymentOutcome.ISSUE_RECORDED
    assert "deactivate" in PaymentIssue.objects.get(reference="reg-abc").detail


@pytest.mark.django_db
def test_a_dispute_changes_nothing_and_tells_the_admin():
    """A dispute can be won. Reversing an order the customer still has would
    be worse than waiting for the outcome."""
    order = _make_order("order-abc")

    outcome = handle_payment_dispute("order-abc", "charge.dispute.create")

    assert outcome == PaymentOutcome.ISSUE_RECORDED
    order.refresh_from_db()
    assert order.status == Order.Status.CONFIRMED
    issue = PaymentIssue.objects.get(reference="order-abc")
    assert issue.kind == PaymentIssue.Kind.DISPUTE_OPENED
    assert "deadline" in issue.detail


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_a_refunded_starter_pack_claws_back_the_sponsors_bonus(mock_verify):
    """User-accepted policy, 2026-09-18: the direct referral bonus only.
    Binary and matching bonuses stay paid -- ADR-0006's reasoning, that a
    pooled PV batch cannot be attributed to one sale, still holds."""
    from apps.wallet.models import WalletTransaction
    from apps.wallet.services import credit as credit_wallet

    sponsor = _make_distributor()
    distributor = _make_distributor(sponsor=sponsor)
    Distributor.objects.filter(pk=distributor.pk).update(
        starter_pack_payment_reference="pack-abc"
    )
    credit_wallet(
        sponsor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="pack-abc",
    )
    mock_verify.return_value = _reversed_verify()

    outcome = handle_external_refund("pack-abc")

    assert outcome == PaymentOutcome.ISSUE_RECORDED
    sponsor.wallet.refresh_from_db()
    assert sponsor.wallet.balance == Decimal("0.00")
    assert WalletTransaction.objects.filter(
        transaction_type=(
            WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS_REVERSAL
        ),
        reference="pack-abc",
    ).exists()


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_a_sponsor_who_already_withdrew_is_not_put_into_debt(mock_verify):
    """User-accepted: log the shortfall, do not chase it. The same rule the
    cooling-off refund already follows."""
    from apps.wallet.models import WalletTransaction
    from apps.wallet.services import credit as credit_wallet
    from apps.wallet.services import debit as debit_wallet

    sponsor = _make_distributor()
    distributor = _make_distributor(sponsor=sponsor)
    Distributor.objects.filter(pk=distributor.pk).update(
        starter_pack_payment_reference="pack-abc"
    )
    credit_wallet(
        sponsor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="pack-abc",
    )
    debit_wallet(
        sponsor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="withdrawal-1",
    )
    mock_verify.return_value = _reversed_verify()

    assert handle_external_refund("pack-abc") == PaymentOutcome.ISSUE_RECORDED

    sponsor.wallet.refresh_from_db()
    assert sponsor.wallet.balance == Decimal("0.00")  # never negative


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_the_clawback_happens_once_not_on_every_replay(mock_verify):
    from apps.wallet.models import WalletTransaction
    from apps.wallet.services import credit as credit_wallet

    sponsor = _make_distributor()
    distributor = _make_distributor(sponsor=sponsor)
    Distributor.objects.filter(pk=distributor.pk).update(
        starter_pack_payment_reference="pack-abc"
    )
    credit_wallet(
        sponsor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="pack-abc",
    )
    mock_verify.return_value = _reversed_verify()

    handle_external_refund("pack-abc")
    handle_external_refund("pack-abc")

    assert (
        WalletTransaction.objects.filter(
            transaction_type=(
                WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS_REVERSAL
            )
        ).count()
        == 1
    )


def _make_distributor(sponsor=None):
    phone = f"+233244{next(_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(
        user=user, phone_number=phone, sponsor=sponsor, full_name="Test Person"
    )


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_a_reference_of_an_odd_shape_never_reaches_a_paystack_url(mock_verify):
    """It lands in /transaction/verify/<reference>; a value carrying "/" or
    ".." would aim that request somewhere else."""
    for reference in ["order-../../secret", "order-x/y", "", "a" * 200]:
        assert handle_external_refund(reference) == PaymentOutcome.UNKNOWN_REFERENCE
        assert handle_payment_dispute(reference, "charge.dispute.create") == (
            PaymentOutcome.UNKNOWN_REFERENCE
        )

    mock_verify.assert_not_called()
    assert not PaymentIssue.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_an_empty_wallet_does_not_get_clawed_back_twice(mock_verify):
    """Paystack sends refund.pending and refund.processed. The first
    clawback correctly debits nothing when the wallet is empty and writes no
    transaction -- so a wallet-row check would run it again, taking money
    the sponsor earned in between (agent code review)."""
    from apps.wallet.models import WalletTransaction
    from apps.wallet.services import credit as credit_wallet
    from apps.wallet.services import debit as debit_wallet

    sponsor = _make_distributor()
    distributor = _make_distributor(sponsor=sponsor)
    Distributor.objects.filter(pk=distributor.pk).update(
        starter_pack_payment_reference="pack-abc"
    )
    credit_wallet(
        sponsor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="pack-abc",
    )
    debit_wallet(
        sponsor,
        Decimal("100.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="withdrawal-1",
    )
    mock_verify.return_value = _reversed_verify()
    handle_external_refund("pack-abc")  # refund.pending: debits nothing

    # The sponsor earns again before the second event arrives.
    credit_wallet(
        sponsor,
        Decimal("80.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="binary-1",
    )
    handle_external_refund("pack-abc")  # refund.processed

    sponsor.wallet.refresh_from_db()
    assert sponsor.wallet.balance == Decimal("80.00")


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_the_clawback_finds_the_bonus_paid_on_an_older_checkout(mock_verify):
    """Adversarial security review. Since 68b the bonus is credited against
    whichever checkout was actually paid, which after a back-button
    re-selection is NOT the distributor's current reference. Looking it up by
    the current one found nothing, took nothing back, and told the admin it
    had worked."""
    from apps.distributors.models import StarterPackCheckout
    from apps.wallet.models import WalletTransaction
    from apps.wallet.services import credit as credit_wallet

    sponsor = _make_distributor()
    distributor = _make_distributor(sponsor=sponsor)
    # Paid the older checkout; the distributor's current field is the newer.
    StarterPackCheckout.objects.create(
        distributor=distributor,
        reference="pack-old",
        amount_pesewas=50000,
        choice="A",
        pv=500,
        rank="bronze",
        consumed_at=timezone.now(),
    )
    Distributor.objects.filter(pk=distributor.pk).update(
        starter_pack_payment_reference="pack-new"
    )
    credit_wallet(
        sponsor,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="pack-old",
    )
    mock_verify.return_value = _reversed_verify()

    handle_external_refund("pack-old")

    sponsor.wallet.refresh_from_db()
    assert sponsor.wallet.balance == Decimal("0.00")
    assert "has been taken back" in (
        PaymentIssue.objects.get(reference="pack-old").detail
    )


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_a_clawback_that_took_nothing_back_says_so(mock_verify):
    """The sponsor had already withdrawn it. That is the accepted outcome --
    but the admin must not be told money came back when it did not."""
    from apps.wallet.models import WalletTransaction
    from apps.wallet.services import credit as credit_wallet
    from apps.wallet.services import debit as debit_wallet

    sponsor = _make_distributor()
    distributor = _make_distributor(sponsor=sponsor)
    Distributor.objects.filter(pk=distributor.pk).update(
        starter_pack_payment_reference="pack-abc"
    )
    credit_wallet(
        sponsor,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.DIRECT_REFERRAL_BONUS,
        reference="pack-abc",
    )
    debit_wallet(
        sponsor,
        Decimal("50.00"),
        transaction_type=WalletTransaction.TransactionType.WITHDRAWAL_DEBIT,
        reference="withdrawal-1",
    )
    mock_verify.return_value = _reversed_verify()

    handle_external_refund("pack-abc")

    assert "could NOT be taken back" in (
        PaymentIssue.objects.get(reference="pack-abc").detail
    )


@pytest.mark.django_db
@patch("apps.distributors.external_refunds.verify_transaction")
def test_a_dispute_after_a_refund_cannot_trigger_a_second_clawback(mock_verify):
    """The double-clawback guard is a durable field, not the PaymentIssue's
    kind -- a dispute event rewrites that kind."""
    from apps.distributors.models import StarterPackCheckout
    from apps.wallet.models import WalletTransaction
    from apps.wallet.services import credit as credit_wallet

    sponsor = _make_distributor()
    distributor = _make_distributor(sponsor=sponsor)
    StarterPackCheckout.objects.create(
        distributor=distributor,
        reference="pack-abc",
        amount_pesewas=50000,
        choice="A",
        pv=500,
        rank="bronze",
        consumed_at=timezone.now(),
    )
    Distributor.objects.filter(pk=distributor.pk).update(
        starter_pack_payment_reference="pack-abc"
    )
    mock_verify.return_value = _reversed_verify()
    handle_external_refund("pack-abc")  # nothing to take back: empty wallet

    handle_payment_dispute("pack-abc", "charge.dispute.create")  # rewrites kind
    credit_wallet(
        sponsor,
        Decimal("80.00"),
        transaction_type=WalletTransaction.TransactionType.BINARY_BONUS,
        reference="binary-1",
    )
    handle_external_refund("pack-abc")  # a redelivered refund event

    sponsor.wallet.refresh_from_db()
    assert sponsor.wallet.balance == Decimal("80.00")
