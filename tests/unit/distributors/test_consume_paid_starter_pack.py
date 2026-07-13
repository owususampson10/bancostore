from itertools import count
from unittest.mock import patch

from django.contrib.auth import get_user_model

import pytest
from constance import config

from apps.distributors.models import Distributor
from apps.distributors.paystack import PaystackError
from apps.distributors.services import consume_paid_starter_pack

User = get_user_model()
_phone_seq = count(1)


def _make_distributor():
    phone = f"+233241{next(_phone_seq):06d}"
    user = User.objects.create_user(username=phone, password="Passw0rd!")
    return Distributor.objects.create(user=user, phone_number=phone)


def _select_pack_b(distributor):
    distributor.starter_pack_choice = "B"
    distributor.starter_pack_price_pesewas = int(config.STARTER_PACK_B_PRICE * 100)
    distributor.starter_pack_pv = config.STARTER_PACK_B_PV
    distributor.starter_pack_rank = config.STARTER_PACK_B_RANK
    distributor.starter_pack_payment_reference = "pack-ref-1"
    distributor.save(
        update_fields=[
            "starter_pack_choice",
            "starter_pack_price_pesewas",
            "starter_pack_pv",
            "starter_pack_rank",
            "starter_pack_payment_reference",
        ]
    )
    return distributor


def _success_verify(amount, currency="GHS"):
    return {"status": "success", "amount": amount, "currency": currency}


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_confirmed_pack_b_purchase_sets_silver_rank_and_1000_pv(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.return_value = _success_verify(amount=200000)  # GHS 2,000

    consume_paid_starter_pack("pack-ref-1")

    distributor.refresh_from_db()
    assert distributor.rank == "silver"
    assert distributor.starter_pack_pv == 1000
    assert distributor.starter_pack_confirmed_at is not None


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_confirmed_pack_a_purchase_sets_bronze_rank_and_500_pv(mock_verify):
    distributor = _make_distributor()
    distributor.starter_pack_choice = "A"
    distributor.starter_pack_price_pesewas = int(config.STARTER_PACK_A_PRICE * 100)
    distributor.starter_pack_pv = config.STARTER_PACK_A_PV
    distributor.starter_pack_rank = config.STARTER_PACK_A_RANK
    distributor.starter_pack_payment_reference = "pack-ref-a"
    distributor.save(
        update_fields=[
            "starter_pack_choice",
            "starter_pack_price_pesewas",
            "starter_pack_pv",
            "starter_pack_rank",
            "starter_pack_payment_reference",
        ]
    )
    mock_verify.return_value = _success_verify(amount=150000)  # GHS 1,500

    consume_paid_starter_pack("pack-ref-a")

    distributor.refresh_from_db()
    assert distributor.rank == "bronze"
    assert distributor.starter_pack_pv == 500


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_already_confirmed_is_an_idempotent_no_op(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.return_value = _success_verify(amount=200000)

    consume_paid_starter_pack("pack-ref-1")
    consume_paid_starter_pack("pack-ref-1")

    distributor.refresh_from_db()
    assert distributor.rank == "silver"
    assert mock_verify.call_count == 1  # second call short-circuits


@pytest.mark.django_db
def test_missing_distributor_reference_does_not_crash():
    consume_paid_starter_pack("no-such-reference")  # must not raise


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_non_success_status_does_not_set_rank(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.return_value = {"status": "failed", "amount": 200000, "currency": "GHS"}

    consume_paid_starter_pack("pack-ref-1")

    distributor.refresh_from_db()
    assert distributor.rank == ""
    assert distributor.starter_pack_confirmed_at is None


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_wrong_currency_does_not_set_rank(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.return_value = _success_verify(amount=200000, currency="NGN")

    consume_paid_starter_pack("pack-ref-1")

    distributor.refresh_from_db()
    assert distributor.rank == ""


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_amount_mismatch_does_not_set_rank(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.return_value = _success_verify(amount=100000)  # wrong amount

    consume_paid_starter_pack("pack-ref-1")

    distributor.refresh_from_db()
    assert distributor.rank == ""


@pytest.mark.django_db
@patch("apps.distributors.services.verify_transaction")
def test_paystack_verify_error_does_not_crash(mock_verify):
    distributor = _select_pack_b(_make_distributor())
    mock_verify.side_effect = PaystackError("timed out")

    consume_paid_starter_pack("pack-ref-1")  # must not raise

    distributor.refresh_from_db()
    assert distributor.rank == ""
