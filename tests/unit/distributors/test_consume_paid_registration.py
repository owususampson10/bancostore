from decimal import Decimal
from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password

import pytest
from constance import config

from apps.distributors.models import Distributor, PendingRegistration
from apps.distributors.paystack import PaystackError
from apps.distributors.services import consume_paid_registration

User = get_user_model()

_phone_seq = count(1)


def _make_sponsor():
    phone = f"+233209{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone, ir_id="IR00001")


def _make_pending(sponsor, reference="ref-1", fee_pesewas=10000, phone=None):
    return PendingRegistration.objects.create(
        full_name="Kofi Mensah",
        phone_number=phone or f"+233241{next(_phone_seq):06d}",
        email="kofi@example.test",
        address="12 Ring Road",
        area="Osu",
        landmark="",
        password_hash=make_password("S3cure-Passw0rd!"),
        sponsor=sponsor,
        payment_reference=reference,
        fee_amount_pesewas=fee_pesewas,
    )


def _success_verify(amount=10000, currency="GHS"):
    return {"status": "success", "amount": amount, "currency": currency}


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_successful_verification_creates_account_and_marks_consumed(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    mock_verify.return_value = _success_verify()

    consume_paid_registration("ref-1")

    pending.refresh_from_db()
    assert pending.consumed_at is not None
    distributor = Distributor.objects.get(phone_number=pending.phone_number)
    assert distributor.sponsor_id == sponsor.id
    assert distributor.full_name == "Kofi Mensah"
    assert distributor.address == "12 Ring Road"
    assert distributor.user.groups.filter(name="distributor").exists()
    assert check_password("S3cure-Passw0rd!", distributor.user.password)


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_already_consumed_is_an_idempotent_no_op(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    mock_verify.return_value = _success_verify()

    consume_paid_registration("ref-1")
    consume_paid_registration("ref-1")  # simulates a duplicate webhook delivery

    assert Distributor.objects.filter(phone_number=pending.phone_number).count() == 1
    # The consumed_at check short-circuits before re-verifying with
    # Paystack, so a duplicate webhook delivery costs no extra API call.
    assert mock_verify.call_count == 1


@pytest.mark.django_db
def test_missing_pending_registration_does_not_crash():
    consume_paid_registration("no-such-reference")

    assert not Distributor.objects.exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_non_success_status_does_not_create_an_account(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    mock_verify.return_value = {"status": "failed", "amount": 10000, "currency": "GHS"}

    consume_paid_registration("ref-1")

    pending.refresh_from_db()
    assert pending.consumed_at is None
    assert not Distributor.objects.filter(phone_number=pending.phone_number).exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_wrong_currency_does_not_create_an_account(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    mock_verify.return_value = _success_verify(currency="NGN")

    consume_paid_registration("ref-1")

    assert not Distributor.objects.filter(phone_number=pending.phone_number).exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_amount_is_checked_against_the_snapshotted_fee_not_live_constance(mock_verify):
    """Regression test for a real bug found via doubt-driven-development: if
    an admin changes REGISTRATION_FEE between initialize and webhook, a
    genuinely, correctly paid registration must not fail because the
    amount is compared against a freshly re-read constance value instead
    of the fee that was actually in effect (and paid) at initialize time."""
    sponsor = _make_sponsor()
    # Snapshotted at (simulated) initialize time, when the fee was 100 GHS.
    pending = _make_pending(sponsor, fee_pesewas=10000)
    # The customer genuinely paid the amount in effect when they checked
    # out (10000 pesewas). Verify confirms that's what Paystack actually
    # processed.
    mock_verify.return_value = _success_verify(amount=10000)

    # An admin changes the live fee *after* checkout but before the webhook
    # arrives. If the amount check re-read this live value instead of the
    # snapshot, this genuinely correct payment would wrongly fail.
    config.REGISTRATION_FEE = Decimal("250")

    consume_paid_registration("ref-1")

    pending.refresh_from_db()
    assert pending.consumed_at is not None
    assert Distributor.objects.filter(phone_number=pending.phone_number).exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_amount_mismatch_does_not_create_an_account(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor, fee_pesewas=10000)
    mock_verify.return_value = _success_verify(amount=5000)

    consume_paid_registration("ref-1")

    assert not Distributor.objects.filter(phone_number=pending.phone_number).exists()


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_existing_distributor_with_same_phone_prevents_duplicate_creation(mock_verify):
    sponsor = _make_sponsor()
    phone = f"+233241{next(_phone_seq):06d}"
    existing_user = User.objects.create_user(username=phone, password="x")
    Distributor.objects.create(user=existing_user, phone_number=phone)
    pending = _make_pending(sponsor, phone=phone)
    mock_verify.return_value = _success_verify()

    consume_paid_registration("ref-1")

    pending.refresh_from_db()
    assert pending.consumed_at is None
    assert Distributor.objects.filter(phone_number=phone).count() == 1


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_paystack_verify_error_does_not_crash(mock_verify):
    sponsor = _make_sponsor()
    pending = _make_pending(sponsor)
    mock_verify.side_effect = PaystackError("timed out")

    consume_paid_registration("ref-1")  # must not raise

    pending.refresh_from_db()
    assert pending.consumed_at is None
